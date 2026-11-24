"""Issue #4248：/faro/collect 客户端中断不得记成服务端 500。

覆盖三条路径：
1. 客户端在途断开（ClientDisconnect）→ 499，无 500 / 无空异常告警；
2. 上游不可达（真故障）→ 502 + warning，语义不变；
3. 访问日志把 499 记 warning、与 5xx ERROR 分离，请求与日志 1:1。
"""
import logging

import pytest
from fastapi import FastAPI
from starlette.requests import ClientDisconnect, Request
from starlette.testclient import TestClient

_LOG = "devops-bot"


def _faro_app(monkeypatch, upstream: str = "http://127.0.0.1:9/collect"):
    from src.api import spa

    monkeypatch.setattr(spa, "_FARO_UPSTREAM", upstream, raising=True)
    app = FastAPI()
    spa.install_spa_routes(app)
    return app


def test_faro_client_disconnect_returns_499_without_error(monkeypatch, caplog):
    """客户端读取请求体时断开 → 499；不打 ERROR、不打空异常 API error。"""
    async def _raise_disconnect(self):
        raise ClientDisconnect()

    monkeypatch.setattr(Request, "body", _raise_disconnect, raising=True)
    client = TestClient(_faro_app(monkeypatch))

    with caplog.at_level(logging.DEBUG, logger="src.api.spa"):
        r = client.post("/faro/collect", content=b"{}",
                        headers={"content-type": "application/json"})

    assert r.status_code == 499
    assert "client disconnected" in r.text
    bad = [rec for rec in caplog.records
           if rec.levelno >= logging.WARNING and "faro collect" in rec.getMessage()]
    assert not bad, [(rec.levelname, rec.getMessage()) for rec in bad]


def test_faro_upstream_unreachable_still_502_and_warns(monkeypatch, caplog):
    """上游不可达是真故障：仍返回 502 并记 warning（含异常类名）。"""
    client = TestClient(_faro_app(monkeypatch))

    with caplog.at_level(logging.DEBUG, logger="src.api.spa"):
        r = client.post("/faro/collect", json={"events": []})

    assert r.status_code == 502
    warnings = [rec for rec in caplog.records
                if rec.levelno == logging.WARNING
                and "faro collect proxy error" in rec.getMessage()]
    assert warnings, [(rec.levelname, rec.getMessage()) for rec in caplog.records]
    # 空异常串也必须带类名（本例为 httpx.ConnectError）
    assert any("Error" in rec.getMessage() for rec in warnings)


def _install_server_common(app):
    from src.api import server_common
    server_common._attach_request_log(app)


def _install_middleware(app):
    from src.api import middleware
    middleware.install_request_middleware(app)


@pytest.mark.parametrize("install", [_install_server_common, _install_middleware])
def test_faro_disconnect_access_log_is_warning_not_error(install, monkeypatch, caplog):
    """访问日志记录一次 status=499 warning（1:1），不出现 5xx ERROR。"""
    async def _raise_disconnect(self):
        raise ClientDisconnect()

    monkeypatch.setattr(Request, "body", _raise_disconnect, raising=True)
    app = _faro_app(monkeypatch)
    install(app)
    client = TestClient(app)

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        r = client.post("/faro/collect", content=b"{}",
                        headers={"content-type": "application/json"})

    assert r.status_code == 499
    records = [rec for rec in caplog.records
               if "/faro/collect" in rec.getMessage() and "status=" in rec.getMessage()]
    assert len(records) == 1, [(rec.levelname, rec.getMessage()) for rec in records]
    rec = records[0]
    assert "status=499" in rec.getMessage()
    assert rec.levelno == logging.WARNING, (rec.levelname, rec.getMessage())
    assert not any("status=500" in x.getMessage() for x in records)