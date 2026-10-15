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
    return {"server": "ok", "server_base_url": base}
