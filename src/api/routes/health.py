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
    try:
        from src.user.db import get_server_base_url
        base = get_server_base_url() or ""
    except Exception:
        base = ""

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

    return {"server": "ok", "server_base_url": base, "agent_runtime": agent_runtime_status}
