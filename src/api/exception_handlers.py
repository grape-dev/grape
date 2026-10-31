"""共享异常处理器（Issue #596 P1：从 main.py 抽出，多入口复用）。

全局异常处理器：异常原文不回显客户端，响应通用文案 + request_id。
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def install_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器（与 main.py 原实现一致）。"""

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        from src.config import log
        # Issue #380/#395: 异常原文（含 DB/SQL 错误细节）不回显给客户端
        log.warning(f"API error ({request.url.path}): {exc}")
        rid = request.scope.get("request_id", "-")
        return JSONResponse(status_code=500, content={
            "code": 500, "message": "服务器内部错误，请稍后重试", "request_id": rid})
