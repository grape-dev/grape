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
            # 单机模式不再无条件放行：/internal/* 里有跨域写端点（xwrite/apply），
            # 无条件放行等于任何能连到该端口的人都能改别的域的数据。
            # 单机部署的内部调用方都是同机进程（回环），所以按来源判定：
            #   回环 → 放行（等价于原来的单机语义）
            #   非回环 → 必须带正确 X-Service-Token；未配置密钥则 fail-closed 403
            client_host = (request.client.host if request.client else "") or ""
            if client_host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
                return await call_next(request)
            if not _service_token.SERVICE_SECRET:
                if not _WARNED_NO_SECRET:
                    log.warning(
                        "single mode: /internal/* reached from non-loopback %s without "
                        "GRAPE_SERVICE_SECRET; rejected. Set GRAPE_SERVICE_SECRET to allow "
                        "cross-host internal calls.", client_host or "unknown",
                    )
                    _WARNED_NO_SECRET = True
                return JSONResponse(
                    status_code=403,
                    content={
                        "code": "INTERNAL_LOOPBACK_ONLY",
                        "detail": "Non-loopback /internal/* requires X-Service-Token "
                                  "(set GRAPE_SERVICE_SECRET) in single mode",
                    },
                )

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
