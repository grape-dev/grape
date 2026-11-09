"""系统健康检查端点——免鉴权基本检查。

供 K8s livenessProbe / nginx 健康检查 / Docker healthcheck 使用。
不查数据库、不暴露系统规模。
"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/health")
def health():
    """免鉴权健康端点——只返回服务是否在线。

    server_base_url（2026-10-15）：CLI/worker 可达的服务端权威基址（meta.server_base_url，
    无则 server-config.json 兜底）。前端 devspace/设备接入用它替代 window.location.origin
    （用户浏览地址 ≠ CLI 可达地址，如生产 www.grapedev.cn vs worker.grapedev.cn）。
    仅供前端构造安装命令，不含敏感信息；查询失败返回空串（前端回退 origin）。
    """
    base = ""
    base_configured = True
    base_warning = ""
    browser_url = ""
    worker_url = ""
    business_url = ""
    split_warning = ""
    try:
        from src.user.db import (get_server_base_url, is_server_base_url_configured,
                                 get_server_base_url_warning,
                                 get_browser_callback_url, get_worker_connect_url,
                                 get_business_api_url, get_url_split_warning)
        base = get_server_base_url() or ""
        base_configured = is_server_base_url_configured()
        base_warning = get_server_base_url_warning() if not base_configured else ""
        # Issue #471 E1：拆分后双地址各取所需（未配新项回退 server_base_url）
        browser_url = get_browser_callback_url() or ""
        worker_url = get_worker_connect_url() or ""
        # Issue #1577：任务内业务 API 基址（未配回退 server_base_url）
        business_url = get_business_api_url() or ""
        split_warning = get_url_split_warning()
    except Exception:
        base = ""
        base_configured = False
        base_warning = "server_base_url 检查失败，请查看服务端日志"
        browser_url = ""
        worker_url = ""
        business_url = ""
        split_warning = ""

    # Issue #452: 暴露 agent_runtime 状态，缺失时显式标记 missing 而非静默通过
    agent_runtime_status = "unknown"
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT name, status, healthy FROM agent_runtimes "
                "WHERE status='online' AND healthy=1 "
                "ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END "
                "LIMIT 1"
            ).fetchone()
            if row:
                agent_runtime_status = "online"
            else:
                # 有 runtime 记录但无 online+healthy 的
                count = c.execute("SELECT COUNT(*) FROM agent_runtimes").fetchone()[0]
                agent_runtime_status = "missing" if count == 0 else "unhealthy"
    except Exception:
        pass

    # Issue #887：skill 一致性门禁聚合项（近 7 天 [skill_mismatch] 拒绝数，AC4）。
    # 免鉴权端点只暴露计数与状态，不含仓库/技能明细；查询失败显式 unknown。
    skill_consistency = {"status": "unknown", "refused_recent": 0, "detail": ""}
    try:
        from src.runtime.health import aggregate_skill_consistency
        _sk = aggregate_skill_consistency()
        skill_consistency = {"status": _sk.get("status", "unknown"),
                             "refused_recent": int(_sk.get("refused_recent") or 0),
                             "detail": _sk.get("detail", "")}
    except Exception:
        skill_consistency["detail"] = "skill consistency aggregate check failed"

    # Issue #455：base_configured/base_warning 供 OSS 用户自检——未配置对外基址时明确告警
    # （OAuth 回调指向 localhost 第三方平台无法回访）。字段新增向后兼容。
    return {
        "server": "ok",
        "server_base_url": base,
        "server_base_url_configured": base_configured,
        "server_base_url_warning": base_warning,
        # Issue #471 E1：浏览器回调 / worker 回连拆分地址（回退 server_base_url）
        "browser_callback_url": browser_url,
        "worker_connect_url": worker_url,
        "task_business_api_url": business_url,
        "url_split_warning": split_warning,
        "agent_runtime": agent_runtime_status,
        "skill_consistency": skill_consistency,
    }
