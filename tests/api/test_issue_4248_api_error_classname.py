"""Issue #4248：全局兜底 API error 日志必须带异常类名，且装配链只有一份实现。

空异常串（如 ClientDisconnect）此前只打印空白，排障无法区分异常类型。

测试按**真实入口装配链**断言：`build_app(...)` 之后再调
`install_exception_handlers(app)`（`main.py` / `grape_server.py` 即在其后补这一步），
因此能覆盖「同一 Exception key 被二次注册覆盖」的缺陷形态。
"""
import logging

from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient

_LOG = "devops-bot"


def _assembled_app(route, exc):
    """按真实入口装配链建 app：build_app 注册 → 入口再调 install_exception_handlers。"""
    from src.api.exception_handlers import install_exception_handlers
    from src.api.server_common import build_app

    app = build_app("issue-4248-test", "0.0")
    install_exception_handlers(app)

    @app.get(route)
    async def _boom():
        raise exc

    return app


def test_build_app_registers_the_single_source_handler():
    """build_app 生效的就是 exception_handlers 里那份唯一实现。"""
    from src.api.server_common import build_app

    app = build_app("issue-4248-test", "0.0")
    handler = app.exception_handlers[Exception]
    assert handler.__qualname__.startswith("install_exception_handlers."), handler.__qualname__


def test_entrypoint_reinstall_is_idempotent_and_does_not_override():
    """入口在 build_app 之后重复装配不得覆盖已注册的 handler。"""
    from src.api.exception_handlers import install_exception_handlers
    from src.api.server_common import build_app

    app = build_app("issue-4248-test", "0.0")
    first = app.exception_handlers[Exception]
    install_exception_handlers(app)
    assert app.exception_handlers[Exception] is first


def test_api_error_log_contains_exception_class(caplog):
    """真故障：500 保持不变，日志补异常类名。"""
    app = _assembled_app("/issue4248-boom", ValueError())
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        r = client.get("/issue4248-boom")

    assert r.status_code == 500
    messages = [rec.getMessage() for rec in caplog.records
                if "API error (/issue4248-boom)" in rec.getMessage()]
    assert messages, [rec.getMessage() for rec in caplog.records]
    assert "ValueError" in messages[0]


def test_client_disconnect_degrades_to_499_info(caplog):
    """客户端断开：降级 499 + info，不落 500 API error。"""
    app = _assembled_app("/issue4248-disconnect", ClientDisconnect())
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        r = client.get("/issue4248-disconnect")

    assert r.status_code == 499
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("API client disconnected (/issue4248-disconnect): ClientDisconnect" in m
               for m in msgs), msgs
    assert not any("API error (/issue4248-disconnect)" in m for m in msgs), msgs
