"""Grape Service Bus — 服务健康检查端点.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.

提供 /api/health 端点，返回服务名、状态、运行时间。
供 GSB 服务注册表心跳检查和外部监控使用。
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter

router = APIRouter(tags=["health"])

# 模块加载时间（近似服务启动时间）
_START_TIME = time.time()


@router.get("/api/health")
async def health_check() -> dict:
    """服务健康检查端点。

    Returns:
        {"service": <name>, "status": "ok", "uptime": <seconds>}
    """
    service_name = os.environ.get("GRAPE_SERVICE_NAME", "unknown")
    return {
        "service": service_name,
        "status": "ok",
        "uptime": int(time.time() - _START_TIME),
    }
