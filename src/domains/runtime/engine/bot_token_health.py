"""Issue #345 方向 1 + R17/R21.3 改造 + Issue #519 异步化。

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

Issue #519 异步化（消除每小时约 74 秒全站卡顿）：
- _verify_repo_bot_tokens 改 async：单仓验证经 asyncio.to_thread 丢入线程池
- 批内 REPO_BATCH_SIZE 个并发（asyncio.gather），批间隔 asyncio.sleep
- 事件循环不再被同步 HTTP 验证 + time.sleep 阻塞，检查期间 API 可正常响应
"""

import asyncio

import json

import random

import time

from src.kernel.config import log

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
    from src.kernel.token_resolver import create_provider
    _vcs = create_provider(provider)

    # D19：全局 bot token 走统一来源链（get_global_bot_token_for，新平台自动支持）
    from src.domains.mgmt.user.db import get_global_bot_token_for
    token = get_global_bot_token_for(provider)

    # R25.4/S96：platform_label 走平台卡 name（卡缺位回退 capitalize——
    # "gitcode".capitalize()="Gitcode" ≠ "GitCode"，卡 name 为权威展示名）
    from src.domains.runtime.vcs.platform_card import get_platform_card
    _card = get_platform_card(provider)
    platform_label = (_card.name if _card is not None and _card.name
                      else provider.capitalize())

    if not token:
        return {"healthy": False, "error": f"{platform_label} token not configured", "error_type": "config"}
    try:
        _vcs.verify_token(token)
        return {"healthy": True, "error": "", "error_type": ""}
    except Exception as e:
        err_msg = str(e)[:200]
        from src.domains.runtime.vcs.token_error import extract_http_status_from_exc
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
    # S96：基集走 provider 翻译模块注册表（=全链路支持的平台，随模块注册扩展），
    # 替代 {gitcode, github} 字面量
    from src.domains.runtime.vcs.providers import _MODULES as _PROVIDER_MODULES
    results = {_p: _verify_bot_token(_p) for _p in _PROVIDER_MODULES}
    # D19：config-only 平台配了全局 bot token（meta global_bot_<p>_token）自动纳入巡检
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms
        from src.domains.mgmt.user.db import get_global_bot_token_for
        for _prov in list_vcs_platforms():
            if _prov in results or not get_global_bot_token_for(_prov):
                continue
            results[_prov] = _verify_bot_token(_prov)
    except Exception:
        pass

    # R17.2.2/R21.2: 标记/清除 per-provider 过期 key
    for _prov, _res in results.items():
        _update_global_token_status(_prov, _res)

    platforms = dict(results)  # S94/D69：gc/gh 死赋值移除（结果已含 config-only 平台）
    return {
        "platforms": platforms,
    }

def _update_global_token_status(provider: str, result: dict):
    """R17.2.2/R17.2.3: 根据 health 检查结果标记/清除 per-provider 过期状态。

    401 (auth) → 标记 global_{provider}_bot_token_status=expired
    200 (healthy) → 清除过期标记
    超时/5xx (network) → 不改变标记状态（等下次检查）
    config (no token) → 不改变标记状态
    """
    from src.domains.mgmt.user.db import set_meta_value, _invalidate_global_bot_cache
    status_key = f"global_{provider}_bot_token_status"

    if result["healthy"]:
        # R17.2.3: 200 → 清除过期标记
        set_meta_value(status_key, "")
        _invalidate_global_bot_cache()
    elif result.get("error_type") == "auth":
        # R17.2.2: 401 → 标记过期
        set_meta_value(status_key, "expired")
        _invalidate_global_bot_cache()

async def _verify_repo_bot_tokens():
    """R17.2.4/R17.2.11: 遍历有仓库级 bot token 的仓库，按 provider 验证。

    Issue #519: 改为 async——单仓验证（_verify_single_repo_token，同步 DB+HTTP）
    经 asyncio.to_thread 丢入线程池执行，批内 asyncio.gather 并发，
    批间隔 asyncio.sleep，检查期间不再阻塞 asyncio 事件循环。

    R17.2.5: 401 → 标记 managed_repos.bot_token_status=expired
    R17.2.6: 200 → 清除过期标记
    R17.2.10: 分批执行（每 10 个一批，间隔 1 秒）
    R21.3.7: 超时/5xx 不标记
    """
    from src.kernel.session_manager import db, db_lock
    from src.kernel.token_resolver import create_provider
    from src.domains.mgmt.user.db import set_meta_value, _invalidate_global_bot_cache

    # 查询有 bot token 的仓库
    repos_to_check = []
    try:
        with db_lock:
            c = db.cursor()
            # S82/D58：凭据唯一权威=grape_t_repo_credentials（managed_repos 列已冻结）
            # S86/D62：provider 由 repo_key 前缀派生（parse_repo_key 单一入口），
            # 字面 cred_type→provider 映射表移除——第 5 平台自动纳入巡检；
            # bot 凭据族过滤保留（webhook_token 不是 bot token，不进巡检）
            from src.kernel.repo_key import parse_repo_key as _prk_health
            # D99：cred_type 归一——bot 凭据族='bot' 精确匹配（LIKE 反模式消除）
            from src.domains.runtime import public as _rt_health
            _rows_cred = None
            if _rt_health.available():
                _rows_cred = [(r[1], r[2]) for r in _rt_health.list_repo_credentials()
                              if r[3] and r[2] == "bot"]
            if _rows_cred is None:
                c.execute(
                    "SELECT repo_key, cred_type FROM grape_t_repo_credentials "
                    "WHERE value IS NOT NULL AND value != '' AND cred_type = 'bot'"
                )
                _rows_cred = c.fetchall()
            for row in _rows_cred:
                repos_to_check.append((row[0], _prk_health(row[0])[0]))
    except Exception as e:
        log.warning(f"_verify_repo_bot_tokens: query failed: {e}")
        return

    if not repos_to_check:
        return

    # Issue #519: 检查任务启动日志（含仓库数，便于监控/对账检查窗口）
    log.info(f"bot_token_health: repo token check started ({len(repos_to_check)} repos)")

    # R17.2.10 + Issue #519: 分批执行，批内并发（asyncio.gather），
    # 单仓验证经 asyncio.to_thread 在线程池执行，不阻塞事件循环
    for i in range(0, len(repos_to_check), REPO_BATCH_SIZE):
        batch = repos_to_check[i:i + REPO_BATCH_SIZE]
        # return_exceptions=True：单仓异常隔离（对齐原逐仓 try/except 语义），
        # 不中断整轮检查；_verify_single_repo_token 内部已 try/except，
        # 此处防御 to_thread 包装层本身抛错（如 loop 关闭竞态）
        results = await asyncio.gather(
            *(asyncio.to_thread(_verify_single_repo_token, repo_key, provider)
              for repo_key, provider in batch),
            return_exceptions=True,
        )
        for (repo_key, provider), res in zip(batch, results):
            if isinstance(res, BaseException):
                log.warning(f"bot_token_health: repo token check failed for {repo_key}: {res}")
        # 批间间隔（Issue #519: asyncio.sleep 让出事件循环，不阻塞调用线程）
        if i + REPO_BATCH_SIZE < len(repos_to_check):
            await asyncio.sleep(REPO_BATCH_INTERVAL)

def _verify_single_repo_token(repo_key: str, provider: str):
    """验证单个仓库级 bot token。

    R17.2.5: 401 → 标记 managed_repos.bot_token_status=expired
    R17.2.6: 200 → 清除过期标记
    R21.3.7: 超时/5xx 不标记
    """
    from src.kernel.session_manager import db, db_lock
    from src.kernel.token_resolver import create_provider

    # 读取仓库级 token：F4/D106 改 rc 优先（grape_t_repo_credentials 唯一权威，
    # cred_type='bot'）；卡列空平台（gitee/gitlab 终态）无列可读。legacy 列
    # 回落仅 gitcode/github 存量行（与 RepoBotToken 同口径，退出 checklist
    # 见 token_resolver——列命中清零后删回落）。
    field = create_provider(provider).token_storage_field()
    token = ""
    try:
        from src.domains.mgmt.user.db import get_repo_credential_value
        token = get_repo_credential_value(repo_key, "bot")
    except Exception as e:
        log.warning(f"_verify_single_repo_token: rc read failed for {repo_key}: {e}")
    if not token and field:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(f"SELECT {field} FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
                row = c.fetchone()
                if row and row[0]:
                    # N3：列值通道加密，verify 前解密（明文兼容覆盖存量行）
                    # legacy 列回落命中（下线判据=零命中）
                    from src.domains.mgmt.user.db import _decrypt_repo_secret
                    log.warning(f"bot_token_health: legacy column fallback hit "
                                f"repo={repo_key} field={field}")
                    token = _decrypt_repo_secret(row[0])
        except Exception as e:
            log.warning(f"_verify_single_repo_token: read token failed for {repo_key}: {e}")
    if not token:
        return  # token 已被清除

    # 验证 token
    _vcs = create_provider(provider)
    try:
        _vcs.verify_token(token)
        # R17.2.6: 200 → 清除过期标记
        _set_repo_bot_token_status(repo_key, "")
    except Exception as e:
        from src.domains.runtime.vcs.token_error import extract_http_status_from_exc
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
    from src.kernel.session_manager import db, db_lock
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
        from src.domains.mgmt.user.db import get_meta_value
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
    from src.domains.mgmt.user.db import set_meta_value

    prev = _get_health_status()
    prev_platforms = (prev or {}).get("platforms", {})

    # 按平台独立计数（S94/D69：遍历 result 内全部已巡检平台，含 config-only）
    platforms_out = {}
    for prov in result.get("platforms", {}):
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
            from src.kernel.lock import acquire_lock, release_lock
            if acquire_lock("token_health_check", ttl=LOCK_TTL):
                try:
                    # R17.2.1: 全局 bot token 检查
                    result = _verify_global_bot_token()
                    _update_health_status(result)
                    # R17.2.4: 仓库级 bot token 检查（Issue #519: async，不阻塞事件循环）
                    await _verify_repo_bot_tokens()
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
