"""GSB 服务基类 + 客户端单元测试（docs/weekly-09 §7.8.4 / §7.8.5）.

覆盖：
- GsbService 注册 / 心跳 / 注销 生命周期
- /internal/gsb/services 与 /internal/gsb/services/{name} 运维端点
- base_url / port 解析优先级（跨机部署不再写死 localhost）
- GsbClient 候选地址顺序与传输层故障切换（env → 注册表 → 本机回退）
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.kernel import session_manager
from src.kernel.gsb import registry
from src.kernel.gsb.client import GsbClient
from src.kernel.gsb.router import (
    GsbService,
    init_gsb,
    resolve_base_url,
    resolve_port,
    service_name_for_role,
)


def _reset_registry() -> None:
    registry._ensure_table()
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute("DELETE FROM gsb_service_registry")
        session_manager.db.commit()


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    # 避免 .env 里的 cluster 模式把 /internal/* 变成必须带服务令牌
    monkeypatch.delenv("GRAPE_MODE", raising=False)
    monkeypatch.delenv("GRAPE_GSB_BASE_URL", raising=False)
    _reset_registry()
    yield
    _reset_registry()


# ---------- 角色映射与地址解析 ----------


def test_role_to_service_name():
    assert service_name_for_role("mgmt") == "mgmt"
    assert service_name_for_role("event") == "event"
    assert service_name_for_role("worker") == "runtime"
    assert service_name_for_role("insight") == "insight"
    # 未来新增服务：未知角色原样透传
    assert service_name_for_role("billing") == "billing"


def test_resolve_port_priority(monkeypatch):
    monkeypatch.delenv("GRAPE_GSB_PORT", raising=False)
    assert resolve_port("mgmt") == 38081
    assert resolve_port("worker") == 38083

    monkeypatch.setenv("GRAPE_GSB_PORT", "39999")
    assert resolve_port("insight") == 39999

    # 显式参数优先级最高
    assert resolve_port("insight", 12345) == 12345


def test_resolve_base_url_priority(monkeypatch):
    monkeypatch.delenv("GRAPE_MGMT_URL", raising=False)
    monkeypatch.delenv("GRAPE_GSB_BASE_URL", raising=False)

    # 1. 服务专属 env
    monkeypatch.setenv("GRAPE_MGMT_URL", "http://10.0.0.5:38081/")
    assert resolve_base_url("mgmt", 38081) == "http://10.0.0.5:38081"

    # 2. 全局 GSB 覆盖优先于服务专属 env
    monkeypatch.setenv("GRAPE_GSB_BASE_URL", "http://mgmt.internal:38081")
    assert resolve_base_url("mgmt", 38081) == "http://mgmt.internal:38081"

    # 3. 显式参数最高
    assert resolve_base_url("mgmt", 38081, base_url="http://explicit:1") == "http://explicit:1"

    # 4. 都没配 → 本机回环 + 实际端口（不是 localhost，跨机可解析）
    monkeypatch.delenv("GRAPE_GSB_BASE_URL")
    monkeypatch.delenv("GRAPE_MGMT_URL")
    assert resolve_base_url("mgmt", 38081) == "http://127.0.0.1:38081"


# ---------- GsbService 生命周期 ----------


def test_init_gsb_mounts_routes_and_state():
    app = FastAPI()
    svc = init_gsb(app, "worker")
    assert isinstance(svc, GsbService)
    assert svc.name == "runtime"
    assert svc.port == 38083
    assert app.state.gsb is svc

    # 路由已挂载：空注册表时返回 count=0（新版 FastAPI 的 include_router 在
    # app.routes 里是 _IncludedRouter 包装，不能靠 r.path 断言，直接打端点）
    client = TestClient(app)
    resp = client.get("/internal/gsb/services")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_service_register_startup_and_shutdown():
    app = FastAPI()
    svc = init_gsb(app, "insight")

    async def cycle():
        await svc.startup()
        return svc

    asyncio.run(cycle())

    row = registry.discover_service("insight")
    assert row["base_url"] == "http://127.0.0.1:38084"
    assert row["health_url"] == "/api/ping"
    assert row["status"] == "online"
    assert '"role": "insight"' in row["metadata"]

    asyncio.run(svc.shutdown())

    assert registry.discover_service("insight") == {}
    rows = registry.list_all_services()
    assert rows[0]["status"] == "offline"


def test_internal_registry_endpoints():
    app = FastAPI()
    svc = init_gsb(app, "mgmt")
    svc.register()
    client = TestClient(app)

    resp = client.get("/internal/gsb/services")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["online"] == 1
    assert body["services"][0]["service_name"] == "mgmt"
    assert body["services"][0]["alive"] is True

    one = client.get("/internal/gsb/services/mgmt")
    assert one.status_code == 200
    assert one.json()["base_url"] == "http://127.0.0.1:38081"

    missing = client.get("/internal/gsb/services/does-not-exist")
    assert missing.status_code == 404


def test_register_failure_does_not_raise(monkeypatch):
    """注册表不可用时，startup 只告警，不阻断进程启动。"""
    app = FastAPI()
    svc = init_gsb(app, "event")

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.kernel.gsb.router.register_service", boom)
    asyncio.run(svc.startup())  # 不应抛出
    assert svc._registered is False

    asyncio.run(svc.shutdown())


# ---------- GsbClient 候选与故障切换 ----------


def test_client_candidate_order_env_then_registry_then_fallback(monkeypatch):
    monkeypatch.setenv("GRAPE_RUNTIME_URL", "http://static:38083")
    registry.register_service("runtime", "http://registry:38083")

    client = GsbClient("runtime", caller_name="mgmt")
    assert client._candidates() == [
        "http://static:38083",
        "http://registry:38083",
        "http://127.0.0.1:38083",
    ]


def test_client_failover_to_registry_on_connect_error(monkeypatch):
    monkeypatch.setenv("GRAPE_RUNTIME_URL", "http://dead-host:38083")
    registry.register_service("runtime", "http://alive-host:38083")

    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class FakeHTTP:
        def request(self, method, url, headers=None, **kwargs):
            calls.append(url)
            if "dead-host" in url:
                raise httpx.ConnectError("connection refused")
            return FakeResponse()

    client = GsbClient("runtime", caller_name="mgmt")
    client._http = FakeHTTP()

    assert client.get("/internal/ping") == {"ok": True}
    assert calls == [
        "http://dead-host:38083/internal/ping",
        "http://alive-host:38083/internal/ping",
    ]


def test_client_does_not_failover_on_http_status_error(monkeypatch):
    """HTTP 4xx/5xx 是业务响应，不应被当作传输故障切到下一个地址。"""
    monkeypatch.setenv("GRAPE_RUNTIME_URL", "http://one-host:38083")
    registry.register_service("runtime", "http://two-host:38083")

    calls: list[str] = []

    class FakeResponse:
        status_code = 500

        def raise_for_status(self):
            raise httpx.HTTPStatusError("500", request=None, response=None)

        def json(self):
            return {}

    class FakeHTTP:
        def request(self, method, url, headers=None, **kwargs):
            calls.append(url)
            return FakeResponse()

    client = GsbClient("runtime", caller_name="mgmt")
    client._http = FakeHTTP()

    with pytest.raises(httpx.HTTPStatusError):
        client.get("/internal/ping")

    assert calls == ["http://one-host:38083/internal/ping"]