"""/internal/* 端点认证中间件（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.7.

仅拦截 /internal/* 路径的请求，验证 X-Service-Token 头。
- 单机模式（GRAPE_MODE != cluster）：自动跳过认证
- 集群模式 + GRAPE_SERVICE_SECRET 已设置：验证 X-Service-Token
- 集群模式 + GRAPE_SERVICE_SECRET 未设置：拒绝请求并告警（fail-closed）
"""
from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.kernel.auth_sdk import service_token as _service_token
from src.kernel.config import log

_WARNED_NO_SECRET = False


class InternalAuthMiddleware(BaseHTTPMiddleware):
    """/internal/* 端点认证中间件。

    - 非以 /internal/ 开头的路径 → 直接放行
    - 单机模式（GRAPE_MODE != cluster）→ 直接放行
    - 集群模式 + SERVICE_SECRET 为空 → 告警并返回 503（fail-closed）
    - 集群模式 → 验证 X-Service-Token 头，失败返回 401
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        global _WARNED_NO_SECRET
        path = request.url.path
        if not path.startswith("/internal/"):
            return await call_next(request)

        is_cluster = os.environ.get("GRAPE_MODE", "").strip().lower() == "cluster"
        if not is_cluster:
            # 单机模式跳过认证
            return await call_next(request)

        if not _service_token.SERVICE_SECRET:
            # 集群模式但密钥未设置——fail-closed，拒绝并告警（仅告警一次避免日志刷屏）
            if not _WARNED_NO_SECRET:
                log.error(
                    "GRAPE_MODE=cluster but GRAPE_SERVICE_SECRET not set; "
                    "/internal/* requests rejected. Set GRAPE_SERVICE_SECRET for inter-domain auth."
                )
                _WARNED_NO_SECRET = True
            return JSONResponse(
                status_code=503,
                content={
                    "code": "SERVICE_SECRET_NOT_CONFIGURED",
                    "detail": "Cluster mode requires GRAPE_SERVICE_SECRET for /internal/* authentication",
                },
            )

        token = request.headers.get("X-Service-Token", "")
        source = request.headers.get("X-Service", "")
        if not _service_token.verify_service_token(token):
            return JSONResponse(
                status_code=401,
                content={"code": "SERVICE_TOKEN_INVALID", "detail": "Invalid or expired service token"},
            )
        # 标记调用方来源（供审计日志使用）
        request.state.service_caller = source
        return await call_next(request)
