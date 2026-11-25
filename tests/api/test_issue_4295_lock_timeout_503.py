"""Issue #4295：db_lock 有界等锁超时 → **503 + Retry-After**（语义与 502 分离）。

现网 19:55:05 ``POST /api/repos/host`` 在 99.9s 持有窗口内被卡 47.9s/56.2s 后返回
**502**，前端只看到"服务异常"、无从重试。修复后的口径：

- 进程内锁等待超预算 = 本机繁忙 → **503** + ``Retry-After``（可重试）；
- 上游 VCS 故障仍是 502（保持不变，本用例不复述）；
- 全局兜底仍返回 500，不被 503 处理器吃掉。
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.exception_handlers import RETRY_AFTER_ENV, install_exception_handlers
from src.kernel import db_lock_metrics as dlm
from src.kernel.config import path_var


def _build_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/ok")
    def ok():
        return {"ok": True}

    @app.get("/hold")
    def hold():
        """模拟"前台读端点持 db_lock"：等锁超预算 → 503。"""
        from src.kernel.session_manager import db_lock
        path_var.set("/api/issues/acme/demo")  # 前台请求上下文（生产由中间件写入）
        with db_lock:
            return {"ok": True}

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    return app


@pytest.fixture
def env_timeout(monkeypatch):
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.2")
    monkeypatch.setenv(RETRY_AFTER_ENV, "3")
    return monkeypatch


def test_lock_wait_timeout_returns_503_with_retry_after(env_timeout):
    from src.kernel.session_manager import db_lock

    ready = threading.Event()
    release = threading.Event()

    def _holder():
        with db_lock:
            ready.set()
            release.wait(5)

    holder = threading.Thread(target=_holder, name="bg-holder", daemon=True)
    holder.start()
    assert ready.wait(5)
    try:
        with TestClient(_build_app()) as client:
            resp = client.get("/hold")
            body = resp.json()
    finally:
        release.set()
        holder.join(5)

    assert resp.status_code == 503, f"锁等待超时必须返回 503（实际 {resp.status_code}）"
    assert resp.headers.get("Retry-After") == "3"
    assert body["code"] == 503
    assert body["reason"] == "db_lock_acquire_timeout"
    assert body["retry_after_s"] == 3
    assert body["request_id"], "响应必须带 request_id，便于与日志/trace 对账"


def test_uncontended_lock_request_is_unaffected(env_timeout):
    with TestClient(_build_app()) as client:
        resp = client.get("/hold")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert "Retry-After" not in resp.headers


def test_generic_error_still_maps_to_500(env_timeout):
    # raise_server_exceptions=False：断言的是"服务端返回什么"，不是 TestClient 重抛
    with TestClient(_build_app(), raise_server_exceptions=False) as client:
        resp = client.get("/boom")
    assert resp.status_code == 500
    assert resp.json()["code"] == 500


def test_retry_after_default_is_used_when_env_unset(monkeypatch):
    monkeypatch.delenv(RETRY_AFTER_ENV, raising=False)
    from src.api.exception_handlers import _retry_after_s
    assert _retry_after_s() == 2
    monkeypatch.setenv(RETRY_AFTER_ENV, "0")  # 下限夹到 1
    assert _retry_after_s() == 1
    monkeypatch.setenv(RETRY_AFTER_ENV, "not-a-number")
    assert _retry_after_s() == 2


def test_install_is_idempotent_and_keeps_single_handler_set():
    app = FastAPI()
    install_exception_handlers(app)
    handlers_after_first = dict(app.exception_handlers)
    install_exception_handlers(app)
    assert dict(app.exception_handlers) == handlers_after_first
    assert dlm.LockAcquireTimeout in app.exception_handlers


def test_timeout_handler_logs_owner_and_holder(env_timeout, caplog):
    """ERROR 日志必须带 waiter/holder/budget —— 现场只有 99.9s 持有者 owner 才可定位。"""
    from src.kernel.session_manager import db_lock

    ready = threading.Event()
    release = threading.Event()

    def _holder():
        with db_lock:
            ready.set()
            release.wait(5)

    holder = threading.Thread(target=_holder, name="bg-holder", daemon=True)
    holder.start()
    assert ready.wait(5)
    try:
        with caplog.at_level("ERROR"):
            with TestClient(_build_app()) as client:
                client.get("/hold")
    finally:
        release.set()
        holder.join(5)

    assert any("db_lock acquire timeout" in r.message for r in caplog.records)
    assert any("holder=" in r.message for r in caplog.records)