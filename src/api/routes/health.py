"""系统健康检查端点——免鉴权基本检查。

供 K8s livenessProbe / nginx 健康检查 / Docker healthcheck 使用。
不查数据库、不暴露系统规模。
"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/health")
async def health():
    """免鉴权健康端点——只返回服务是否在线。"""
    return {"server": "ok"}
