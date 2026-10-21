"""Bot Token 健康探活（Issue #345 方向 1 + R17/R21.3 改造）。

集群部署下，定时探活全局/仓库级 bot token 有效性，结果写入 DB meta 表供所有实例共享。

R17 改造要点：
- CHECK_INTERVAL 300→3600（1 小时），FAILURE_THRESHOLD 3→1（1 次 401 即标记）
- _verify_bot_token 改为通过 VCSProvider.verify_token() 调用（R18.5.8）
- 新增仓库级 token 遍历检查（R17.2.4/R17.2.11）
- 401 → 标记 per-provider 过期 key（R21.2）；200 → 清除过期标记
- 仓库级检查分批执行（每 10 个一批，间隔 1 秒，R17.2.10）
- 分布式锁确保多实例只有一个执行（R17.2.9）

R21.3 超时误标记修复：
- 401 → 标记过期（token 确实无效）
- 超时/5xx → 不标记，记 warning，等下次健康检查重试
- FAILURE_THRESHOLD=1 只对 401 生效
"""
import asyncio
import json
import random
import time

from src.config import log


# R17.2.1: CHECK_INTERVAL 300→3600（1 小时），FAILURE_THRESHOLD 3→1
CHECK_INTERVAL = 3600  # 1 小时
LOCK_TTL = 3000        # 锁过期 < 探活间隔，确保下次探活前锁已释放
FAILURE_THRESHOLD = 1  # R21.3.4: 1 次 401 即标记（只对 401，超时/5xx 不计）

# R17.2.10: 仓库级检查分批
REPO_BATCH_SIZE = 10
REPO_BATCH_INTERVAL = 1  # 秒


def _verify_bot_token(provider: str) -> dict:
    """验证指定平台的全局 bot token 有效性。

    R18.5.8: 通过 VCSProvider.verify_token() 调用，不直接调 _req / _github_req。
    R21.3: 区分 401（真过期）和超时/5xx（网络问题），只对 401 返回 auth 错误。

    Args:
        provider: "gitcode" 或 "github"

    Returns:
        {"healthy": bool, "error": str, "error_type": str}
        error_type: "auth" (401) / "network" (timeout, 5xx) / "config" (no token) / "" (healthy)
    """
    from src.token_resolver import create_provider
    _vcs = create_provider(provider)

    if provider == "github":
        from src.user.db import get_global_github_bot_token
        token = get_global_github_bot_token()
    else:
        from src.user.db import get_global_gitcode_bot_token
        token = get_global_gitcode_bot_token()

    # R25.4: platform_label 用映射表，不依赖 capitalize()（"gitcode".capitalize()="Gitcode" ≠ "GitCode"）
    _PLATFORM_LABELS = {"gitcode": "GitCode", "github": "GitHub"}
    platform_label = _PLATFORM_LABELS.get(provider, provider.capitalize())

    if not token:
        return {"healthy": False, "error": f"{platform_label} token not configured", "error_type": "config"}
    try:
        _vcs.verify_token(token)
        return {"healthy": True, "error": "", "error_type": ""}
    except Exception as e:
        err_msg = str(e)[:200]
        from src.vcs.token_error import extract_http_status_from_exc
        http_code = extract_http_status_from_exc(e)
        if http_code == 401:
            # R21.3.1: 401 → token 确实无效
            return {"healthy": False, "error": err_msg, "error_type": "auth"}
        # R21.3.2: 超时/5xx → 网络问题，不标记过期
        return {"healthy": False, "error": err_msg, "error_type": "network"}


def _verify_global_bot_token() -> dict:
    """探活所有平台的全局 bot token，返回分平台明细。

    R17.2.2/R21.2: 401 → 标记 per-provider `global_*_bot_token_status=expired`
    R17.2.3: 200 → 清除过期标记
    """
    gc = _verify_bot_token("gitcode")
    gh = _verify_bot_token("github")

    # R17.2.2/R21.2: 标记/清除 per-provider 过期 key
    _update_global_token_status("gitcode", gc)
    _update_global_token_status("github", gh)

    return {
        "platforms": {
            "gitcode": gc,
            "github": gh,
        }
    }


def _update_global_token_status(provider: str, result: dict):
    """R17.2.2/R17.2.3: 根据 health 检查结果标记/清除 per-provider 过期状态。

    401 (auth) → 标记 global_{provider}_bot_token_status=expired
    200 (healthy) → 清除过期标记
    超时/5xx (network) → 不改变标记状态（等下次检查）
    config (no token) → 不改变标记状态
    """
    from src.user.db import set_meta_value, _invalidate_global_bot_cache
    status_key = f"global_{provider}_bot_token_status"

    if result["healthy"]:
        # R17.2.3: 200 → 清除过期标记
        set_meta_value(status_key, "")
        _invalidate_global_bot_cache()
    elif result.get("error_type") == "auth":
        # R17.2.2: 401 → 标记过期
        set_meta_value(status_key, "expired")
        _invalidate_global_bot_cache()
    # network/config → 不改变


def _verify_repo_bot_tokens():
    """R17.2.4/R17.2.11: 遍历有仓库级 bot token 的仓库，按 provider 验证。

    R17.2.5: 401 → 标记 managed_repos.bot_token_status=expired
    R17.2.6: 200 → 清除过期标记
    R17.2.10: 分批执行（每 10 个一批，间隔 1 秒）
    R21.3.7: 超时/5xx 不标记
    """
    from src.session_manager import db, db_lock
    from src.token_resolver import create_provider
    from src.user.db import set_meta_value, _invalidate_global_bot_cache

    # 查询有 bot token 的仓库
    repos_to_check = []
    try:
        with db_lock:
            c = db.cursor()
            # GitCode 仓库级 token
            c.execute(
                "SELECT repo_key FROM managed_repos "
                "WHERE bot_gitcode_token IS NOT NULL AND bot_gitcode_token!='' "
                "AND repo_key NOT LIKE 'github:%'"
            )
            for row in c.fetchall():
                repos_to_check.append((row[0], "gitcode"))
            # GitHub 仓库级 token
            c.execute(
                "SELECT repo_key FROM managed_repos "
                "WHERE bot_github_token IS NOT NULL AND bot_github_token!='' "
                "AND repo_key LIKE 'github:%'"
            )
            for row in c.fetchall():
                repos_to_check.append((row[0], "github"))
    except Exception as e:
        log.warning(f"_verify_repo_bot_tokens: query failed: {e}")
        return

    if not repos_to_check:
        return

    log.info(f"bot_token_health: checking {len(repos_to_check)} repo-level tokens")

    # R17.2.10: 分批执行
    for i in range(0, len(repos_to_check), REPO_BATCH_SIZE):
        batch = repos_to_check[i:i + REPO_BATCH_SIZE]
        for repo_key, provider in batch:
            try:
                _verify_single_repo_token(repo_key, provider)
            except Exception as e:
                log.warning(f"bot_token_health: repo token check failed for {repo_key}: {e}")
        # 批间间隔
        if i + REPO_BATCH_SIZE < len(repos_to_check):
            time.sleep(REPO_BATCH_INTERVAL)


def _verify_single_repo_token(repo_key: str, provider: str):
    """验证单个仓库级 bot token。

    R17.2.5: 401 → 标记 managed_repos.bot_token_status=expired
    R17.2.6: 200 → 清除过期标记
    R21.3.7: 超时/5xx 不标记
    """
    from src.session_manager import db, db_lock
    from src.token_resolver import create_provider

    # 读取仓库级 token
    field = "bot_github_token" if provider == "github" else "bot_gitcode_token"
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT {field} FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            row = c.fetchone()
            if not row or not row[0]:
                return  # token 已被清除
            token = row[0]
    except Exception as e:
        log.warning(f"_verify_single_repo_token: read token failed for {repo_key}: {e}")
        return

    # 验证 token
    _vcs = create_provider(provider)
    try:
        _vcs.verify_token(token)
        # R17.2.6: 200 → 清除过期标记
        _set_repo_bot_token_status(repo_key, "")
    except Exception as e:
        from src.vcs.token_error import extract_http_status_from_exc
        http_code = extract_http_status_from_exc(e)
        if http_code == 401:
            # R17.2.5: 401 → 标记过期
            _set_repo_bot_token_status(repo_key, "expired")
            log.warning(f"bot_token_health: repo {repo_key} bot token expired (401)")
        else:
            # R21.3.7: 超时/5xx → 不标记
            log.warning(f"bot_token_health: repo {repo_key} verify error (non-401, skipped): {str(e)[:100]}")


def _set_repo_bot_token_status(repo_key: str, status: str):
    """更新 managed_repos.bot_token_status。"""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE managed_repos SET bot_token_status=? WHERE repo_key=?",
                (status, repo_key),
            )
            db.commit()
    except Exception as e:
        log.warning(f"_set_repo_bot_token_status: update failed for {repo_key}: {e}")


def _get_health_status() -> dict | None:
    """从 DB meta 表读取上次健康状态。"""
    try:
        from src.user.db import get_meta_value
        raw = get_meta_value("bot_token_health")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


def _update_health_status(result: dict):
    """更新健康状态到 DB meta 表（admin 页展示用，保留三级状态）。

    R21.3.4: FAILURE_THRESHOLD=1 只对 401（auth）生效，超时/5xx（network）不计入。
    """
    from src.user.db import set_meta_value

    prev = _get_health_status()
    prev_platforms = (prev or {}).get("platforms", {})

    # 按平台独立计数
    platforms_out = {}
    for prov in ("gitcode", "github"):
        pr = result["platforms"].get(prov, {})
        if pr.get("error_type") == "config":
            continue  # 未配 token 的平台跳过，不计数、不纳入聚合
        prev_p = prev_platforms.get(prov, {})
        prev_failures = prev_p.get("consecutive_failures", 0) if not prev_p.get("healthy") else 0
        if pr["healthy"]:
            failures = 0
        elif pr.get("error_type") == "auth":
            # R21.3.4: 401 直接计入 FAILURE_THRESHOLD
            failures = prev_failures + 1
        else:
            # R21.3.4: 超时/5xx 不计入 FAILURE_THRESHOLD
            failures = prev_failures
        platforms_out[prov] = {
            "healthy": pr["healthy"],
            "degraded": not pr["healthy"] and failures < FAILURE_THRESHOLD,
            "unhealthy": not pr["healthy"] and failures >= FAILURE_THRESHOLD,
            "consecutive_failures": failures,
            "error_type": pr.get("error_type", ""),
            "error": pr.get("error", ""),
        }

    # WorstOf 聚合
    if not platforms_out:
        overall = "healthy"  # 没有平台配置 token
    elif all(p["healthy"] for p in platforms_out.values()):
        overall = "healthy"
    elif all(p["unhealthy"] for p in platforms_out.values()):
        overall = "unhealthy"
    else:
        overall = "degraded"

    status = {
        "overall": overall,
        "platforms": platforms_out,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    set_meta_value("bot_token_health", json.dumps(status))

    # 状态翻转通知
    prev_overall = (prev or {}).get("overall", "healthy")
    if prev_overall == "healthy" and overall != "healthy":
        degraded_provs = [prov for prov, p in platforms_out.items() if not p["healthy"]]
        log.warning(f"bot_token_health: STATUS FLIP healthy→{overall} "
                    f"(platforms: {','.join(degraded_provs)})")
    elif prev_overall != "healthy" and overall == "healthy":
        log.info(f"bot_token_health: STATUS RECOVERED {prev_overall}→healthy")

    if overall == "healthy":
        log.info("bot_token_health: healthy")
    else:
        for prov, p in platforms_out.items():
            if p["unhealthy"]:
                log.warning(f"bot_token_health: {prov} UNHEALTHY (consecutive failures={p['consecutive_failures']}, "
                            f"type={p['error_type']}): {p['error']}")
            elif p["degraded"]:
                log.warning(f"bot_token_health: {prov} degraded (consecutive failures={p['consecutive_failures']}, "
                            f"type={p['error_type']}): {p['error']}")


async def bot_token_health_loop():
    """R17.2: 每小时探活全局 + 仓库级 bot token，集群下只一个实例执行。"""
    log.info(f"Bot token health loop started ({CHECK_INTERVAL}s interval)")
    while True:
        try:
            from src.lock import acquire_lock, release_lock
            if acquire_lock("token_health_check", ttl=LOCK_TTL):
                try:
                    # R17.2.1: 全局 bot token 检查
                    result = _verify_global_bot_token()
                    _update_health_status(result)
                    # R17.2.4: 仓库级 bot token 检查
                    _verify_repo_bot_tokens()
                finally:
                    release_lock("token_health_check")
            # 未抢到锁 = 其他实例在探活，跳过
        except asyncio.CancelledError:
            log.info("Bot token health loop cancelled")
            raise
        except Exception as e:
            log.warning(f"bot_token_health loop error: {e}")
        await asyncio.sleep(CHECK_INTERVAL + random.uniform(0, 60))


def start_bot_token_health():
    """幂等启动探活任务。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("bot_token_health: no running event loop, not started")
        return None
    # 幂等：已有同协程任务则不重复启动
    for t in asyncio.all_tasks(loop):
        coro = t.get_coro()
        if coro is not None and getattr(coro, "__qualname__", "") == bot_token_health_loop.__qualname__ and not t.done():
            return t
    task = loop.create_task(bot_token_health_loop(), name="bot-token-health")
    return task
