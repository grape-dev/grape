"""/internal/* 端点认证中间件（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.7.

仅拦截 /internal/* 路径的请求，验证 X-Service-Token 头。
单机模式（GRAPE_SERVICE_SECRET 未设置）自动跳过认证。
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.kernel.auth_sdk.service_token import verify_service_token, SERVICE_SECRET


class InternalAuthMiddleware(BaseHTTPMiddleware):
    """/internal/* 端点认证中间件。

    - 非以 /internal/ 开头的路径 → 直接放行
    - 单机模式（SERVICE_SECRET 为空）→ 直接放行
    - 集群模式 → 验证 X-Service-Token 头，失败返回 401
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if not path.startswith("/internal/"):
            return await call_next(request)
        if not SERVICE_SECRET:
            # 单机模式跳过认证
            return await call_next(request)
        token = request.headers.get("X-Service-Token", "")
        source = request.headers.get("X-Service", "")
        if not verify_service_token(token):
            return JSONResponse(
                status_code=401,
                content={"code": "SERVICE_TOKEN_INVALID", "detail": "Invalid or expired service token"},
            )
        # 标记调用方来源（供审计日志使用）
        request.state.service_caller = source
        return await call_next(request)
