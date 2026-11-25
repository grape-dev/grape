"""共享异常处理器（Issue #596 P1：从 main.py 抽出，多入口复用）。

全局异常处理器：异常原文不回显客户端，响应通用文案 + request_id。

Issue #4248：全局兜底处理器的**唯一实现**在本模块。各入口（``build_app`` 以及
``main.py`` / ``grape_server.py`` 等在其后补的装配）统一经
``install_exception_handlers`` 注册，**禁止**在调用方再内联一份
``@app.exception_handler(Exception)``：FastAPI 以异常类为 key 存 handler，后注册会
静默覆盖先注册，导致改动只落在不生效的那份实现上。重复装配按幂等处理。
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

_INSTALLED_ATTR = "_grape_exception_handlers_installed"


def install_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器（唯一注册点；对同一 app 幂等）。"""

    if getattr(app.state, _INSTALLED_ATTR, False):
        return
    setattr(app.state, _INSTALLED_ATTR, True)

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        from src.kernel.config import log
        rid = request.scope.get("request_id", "-")
        # Issue #4248：空异常串（``ClientDisconnect`` 的 str 为空）会让排障出现盲区，
        # 日志至少带上异常类名。
        exc_name = type(exc).__name__
        if isinstance(exc, ClientDisconnect):
            # 客户端主动中止（页面卸载/切换，nginx 记 499）不是服务端故障：
            # 降级 info 并返回 499，不污染 5xx 错误率与 ERROR 日志。
            log.info(f"API client disconnected ({request.url.path}): {exc_name}")
            return JSONResponse(status_code=499, content={
                "code": 499, "message": "客户端已断开", "request_id": rid})
        # Issue #380/#395: 异常原文（含 DB/SQL 错误细节）不回显给客户端
        log.warning(f"API error ({request.url.path}): {exc_name}: {exc}")
        return JSONResponse(status_code=500, content={
            "code": 500, "message": "服务器内部错误，请稍后重试", "request_id": rid})
