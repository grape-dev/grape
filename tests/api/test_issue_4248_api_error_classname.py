"""Issue #4248：全局兜底 API error 日志必须带异常类名。

空异常串（如 ClientDisconnect）此前只打印空白，排障无法区分异常类型。
"""
import logging

from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient

_LOG = "devops-bot"


def _app_with(monkeypatch, route, exc):
    from src.api.server_common import build_app

    app = build_app("issue-4248-test", "0.0")

    @app.get(route)
    async def _boom():
        raise exc

    return app


def test_api_error_log_contains_exception_class(monkeypatch, caplog):
    """真故障：500 保持不变，日志补异常类名。"""
    app = _app_with(monkeypatch, "/issue4248-boom", ValueError())
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        r = client.get("/issue4248-boom")

    assert r.status_code == 500
    messages = [rec.getMessage() for rec in caplog.records
                if "API error (/issue4248-boom)" in rec.getMessage()]
    assert messages, [rec.getMessage() for rec in caplog.records]
    assert "ValueError" in messages[0]


def test_client_disconnect_degrades_to_499_info(monkeypatch, caplog):
    """客户端断开：降级 499 + info，不落 500 API error。"""
    app = _app_with(monkeypatch, "/issue4248-disconnect", ClientDisconnect())
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        r = client.get("/issue4248-disconnect")

    assert r.status_code == 499
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("API client disconnected (/issue4248-disconnect): ClientDisconnect" in m
               for m in msgs), msgs
    assert not any("API error (/issue4248-disconnect)" in m for m in msgs), msgs