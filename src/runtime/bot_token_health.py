"""Bot Token 健康探活（Issue #345 方向 1）。

集群部署下，定时探活全局 bot token 有效性，结果写入 DB meta 表供所有实例共享。

设计要点：
- 分布式锁：用 src/lock.py 的 distributed_locks 表（有真正 TTL，实例崩溃自动释放）
  确保集群中只有一个实例执行探活
- 健康状态存 DB：不存内存（集群下每次探活可能是不同实例）
- 连续 3 次失败才标记 unhealthy，期间 degraded（防网络抖动误判）
- 只发现不干预：探活只标记状态，不停用 token，写操作的 401 由方向 3 处理
"""
import asyncio
import json
import random
import time

from src.config import log


CHECK_INTERVAL = 300  # 5 分钟
LOCK_TTL = 240        # 锁过期 < 探活间隔，确保下次探活前锁已释放
FAILURE_THRESHOLD = 3  # 连续失败 3 次才标记 unhealthy


def _verify_bot_token(provider: str) -> dict:
    """验证指定平台的 bot token 有效性。

    Args:
        provider: "gitcode" 或 "github"

    Returns:
        {"healthy": bool, "error": str, "error_type": str}
        error_type: "auth" (401) / "network" (timeout, connection) / "config" (no token) / "" (healthy)
    """
    if provider == "github":
        from src.user.db import get_global_github_bot_token
        token = get_global_github_bot_token()
        api_url = "https://api.github.com/user"
        platform_label = "GitHub"
    else:
        from src.user.db import get_global_gitcode_bot_token
        token = get_global_gitcode_bot_token()
        api_url = "https://gitcode.com/api/v5/user"
        platform_label = "GitCode"

    if not token:
        return {"healthy": False, "error": f"{platform_label} token not configured", "error_type": "config"}
    try:
        if provider == "github":
            from src.user.repo_manager import _github_req
            _github_req("GET", api_url, token)
        else:
            from src.user.repo_manager import _req
            _req("GET", api_url, token)
        return {"healthy": True, "error": "", "error_type": ""}
    except Exception as e:
        err_msg = str(e)[:200]
        from src.vcs.token_error import extract_http_status_from_exc
        http_code = extract_http_status_from_exc(e)
        if http_code == 401:
            return {"healthy": False, "error": err_msg, "error_type": "auth"}
        return {"healthy": False, "error": err_msg, "error_type": "network"}


def _verify_global_bot_token() -> dict:
    """探活所有平台的 bot token，返回分平台明细。

    返回格式:
        {"platforms": {"gitcode": {healthy, error, error_type}, "github": {...}}}

    config 类错误（token 未配置）的平台仍返回结果，由 _update_health_status 跳过。
    """
    gc = _verify_bot_token("gitcode")
    gh = _verify_bot_token("github")
    return {
        "platforms": {
            "gitcode": gc,
            "github": gh,
        }
    }


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
    """更新健康状态到 DB meta 表。

    按平台独立失败计数（集群下每次探活可能是不同实例）。
    config 类错误（token 未配置）的平台跳过——不探活、不计数、不纳入聚合。
    error_type 记录最近一次失败的类型（auth/network/config），admin 据此区分处置路径：
    - auth (401): token 被吊销，去对应平台重新生成
    - network: 网络问题，排查网络/代理/防火墙
    - config: token 未配置（不纳入聚合，仅记录）

    聚合规则（WorstOf）:
    - overall = healthy: 所有已配平台都健康
    - overall = degraded: 部分已配平台故障
    - overall = unhealthy: 全部已配平台都达到 unhealthy 阈值
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
        else:
            failures = prev_failures + 1
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
        overall = "unhealthy"  # 全部已配平台都达到 unhealthy 阈值
    else:
        overall = "degraded"  # 部分平台故障（含 healthy+unhealthy 混合）

    status = {
        "overall": overall,
        "platforms": platforms_out,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    set_meta_value("bot_token_health", json.dumps(status))

    # P2-4: 状态翻转通知——healthy→degraded/unhealthy 时 log.warning（运维可接日志告警）
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
    """每 5 分钟探活全局 bot token，集群下只一个实例执行。"""
    log.info("Bot token health loop started (5min interval)")
    while True:
        try:
            from src.lock import acquire_lock, release_lock
            if acquire_lock("bot_token_health", ttl=LOCK_TTL):
                try:
                    result = _verify_global_bot_token()
                    _update_health_status(result)
                finally:
                    release_lock("bot_token_health")
            # 未抢到锁 = 其他实例在探活，跳过
        except asyncio.CancelledError:
            log.info("Bot token health loop cancelled")
            raise
        except Exception as e:
            log.warning(f"bot_token_health loop error: {e}")
        await asyncio.sleep(CHECK_INTERVAL + random.uniform(0, 30))


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
