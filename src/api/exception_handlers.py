"""共享异常处理器（Issue #596 P1：从 main.py 抽出，多入口复用）。

全局异常处理器：异常原文不回显客户端，响应通用文案 + request_id。

Issue #4248：全局兜底处理器的**唯一实现**在本模块。各入口（``build_app`` 以及
``main.py`` / ``grape_server.py`` 等在其后补的装配）统一经
``install_exception_handlers`` 注册，**禁止**在调用方再内联一份
``@app.exception_handler(Exception)``：FastAPI 以异常类为 key 存 handler，后注册会
静默覆盖先注册，导致改动只落在不生效的那份实现上。重复装配按幂等处理。

Issue #4295：新增 ``LockAcquireTimeout`` 专用处理器——进程级 ``db_lock`` 等锁超预算
是**本进程繁忙**（可重试），与「上游 VCS 故障」的 502 语义分离，统一返回
**503 + ``Retry-After``** 并记录带 owner/holder 的 ERROR，前端据此做"稍后自动重试"。
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from src.kernel.db_lock_metrics import LockAcquireTimeout

_INSTALLED_ATTR = "_grape_exception_handlers_installed"

RETRY_AFTER_ENV = "DB_LOCK_RETRY_AFTER_S"
_RETRY_AFTER_DEFAULT_S = 2


def _retry_after_s() -> int:
    """锁等待超时后的建议重试间隔（秒，``DB_LOCK_RETRY_AFTER_S``，默认 2）。"""
    try:
        import os
        raw = os.environ.get(RETRY_AFTER_ENV, "")
        val = int(float(raw)) if str(raw).strip() != "" else _RETRY_AFTER_DEFAULT_S
    except (TypeError, ValueError):
        val = _RETRY_AFTER_DEFAULT_S
    return max(1, val)


def install_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器（唯一注册点；对同一 app 幂等）。"""

    if getattr(app.state, _INSTALLED_ATTR, False):
        return
    setattr(app.state, _INSTALLED_ATTR, True)

    @app.exception_handler(LockAcquireTimeout)
    async def db_lock_timeout_handler(request: Request, exc: LockAcquireTimeout):
        """db_lock 有界等锁超时 → 503 + Retry-After（Issue #4295）。

        不再落进 500 兜底：503 让前端/调用方可辨别"稍后重试"而非"服务异常"，
        ``Retry-After`` 给出可执行的等待秒数；响应体带 reason 与 request_id 便于对账。
        """
        from src.kernel.config import log
        rid = request.scope.get("request_id", "-")
        retry_after = _retry_after_s()
        log.error(
            f"API db_lock acquire timeout ({request.url.path}): "
            f"waiter={exc.owner} waited={exc.waited:.2f}s budget={exc.timeout:.2f}s "
            f"holder={exc.holder} request_id={rid}")
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(retry_after)},
            content={
                "code": 503,
                "message": "服务繁忙（数据库锁等待超时），请稍后重试",
                "reason": "db_lock_acquire_timeout",
                "retry_after_s": retry_after,
                "request_id": rid,
            },
        )

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