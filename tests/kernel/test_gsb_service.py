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
    # 避免 .env 里的部署开关泄漏进用例：cluster 模式会让 /internal/* 需要令牌，
    # remote/namespace 会改变注册键与 provider 选择（用例自带显式设置）
    for _k in (
        "GRAPE_MODE",
        "GRAPE_GSB_BASE_URL",
        "GRAPE_PROVIDER_MODE",
        "GRAPE_GSB_NAMESPACE",
        "GRAPE_MGMT_URL",
        "GRAPE_RUNTIME_URL",
        "GRAPE_INSIGHT_URL",
        "GRAPE_EVENT_URL",
    ):
        monkeypatch.delenv(_k, raising=False)
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

def test_client_post_does_not_failover_on_read_timeout(monkeypatch):
    """非幂等请求在读超时后不得切候选重放（请求可能已被服务端执行）。"""
    monkeypatch.setenv("GRAPE_RUNTIME_URL", "http://dead-host:38083")
    registry.register_service("runtime", "http://alive-host:38083")

    calls: list[str] = []

    class FakeHTTP:
        def request(self, method, url, headers=None, **kwargs):
            calls.append(url)
            raise httpx.ReadTimeout("read timed out")

    client = GsbClient("runtime", caller_name="mgmt")
    client._http = FakeHTTP()

    with pytest.raises(httpx.ReadTimeout):
        client.post("/internal/issues/state", json_data={"state": "done"})

    assert calls == ["http://dead-host:38083/internal/issues/state"]


def test_client_post_failovers_on_connect_error(monkeypatch):
    """连接期失败可安全切换：请求还没到服务端。"""
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

    assert client.post("/internal/issues/state", json_data={}) == {"ok": True}
    assert calls == [
        "http://dead-host:38083/internal/issues/state",
        "http://alive-host:38083/internal/issues/state",
    ]


def test_client_get_failovers_on_read_timeout(monkeypatch):
    """幂等 GET 读超时仍可切候选（无重复副作用）。"""
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
                raise httpx.ReadTimeout("read timed out")
            return FakeResponse()

    client = GsbClient("runtime", caller_name="mgmt")
    client._http = FakeHTTP()

    assert client.get("/internal/ping") == {"ok": True}
    assert calls == [
        "http://dead-host:38083/internal/ping",
        "http://alive-host:38083/internal/ping",
    ]


def test_heartbeat_loop_runs_periodic_purge(monkeypatch):
    """心跳循环每 PURGE_EVERY_N_BEATS 拍收敛一次注册表（死进程行标 offline）。"""
    purged: list[int] = []
    monkeypatch.setattr(
        "src.kernel.gsb.router.purge_stale",
        lambda: purged.append(1) or 0,
        raising=True,
    )
    monkeypatch.setattr("src.kernel.gsb.router.PURGE_EVERY_N_BEATS", 1, raising=True)
    monkeypatch.setattr(
        "src.kernel.gsb.router.heartbeat_service", lambda name: None, raising=True
    )

    app = FastAPI()
    svc = init_gsb(app, "mgmt")
    svc._registered = True

    async def _stop(_delay):
        raise asyncio.CancelledError()

    monkeypatch.setattr("src.kernel.gsb.router.asyncio.sleep", _stop, raising=True)

    asyncio.run(svc._heartbeat_loop())
    assert len(purged) == 1


def test_get_service_returns_remote_when_mode_remote(monkeypatch):
    """GRAPE_PROVIDER_MODE=remote 时按方法路由（Issue #2479）。

    get_service 返回 _RoutedService 代理；未配置路由表时按内置基线取值：
    压测超阈值方法（get_bot_config）与热路径方法（verify_session_token）
    均锁 lib，采样错误方法（query_metrics）默认偏好 remote。
    """
    from src.kernel.gsb.provider import get_service, decide_mode

    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    monkeypatch.delenv("GRAPE_PROCESS_ROLE", raising=False)
    monkeypatch.delenv("GRAPE_SERVICE_NAME", raising=False)
    svc = get_service("mgmt")
    assert type(svc).__name__ == "_RoutedService"
    # 内置基线（QA 审查修复 Required#1）：压测超阈值项基线锁 lib（get_bot_config
    # 增幅 >20%），采样错误项默认偏好 remote，热路径方法锁 lib
    assert decide_mode("mgmt", "get_bot_config") == "lib"
    assert decide_mode("insight", "query_metrics") == "remote"
    assert decide_mode("mgmt", "verify_session_token") == "lib"


def test_get_local_service_ignores_remote_mode(monkeypatch):
    """/internal/* 服务端必须强制 Local：否则 remote 模式下会 HTTP 回环自调用。"""
    from src.kernel.gsb.provider import get_local_service, is_remote_mode

    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    assert is_remote_mode() is True
    svc = get_local_service("mgmt")
    assert type(svc).__name__ == "MgmtServiceLocal"


def test_internal_routes_use_local_provider_only():
    """结构性守卫：三域 /internal/* 不得再引用模式敏感的 get_*_service()。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "domains"
    for domain in ("mgmt", "runtime", "insight"):
        src = (root / domain / "routes" / "internal.py").read_text(encoding="utf-8")
        for banned in ("get_mgmt_service()", "get_runtime_service()", "get_insight_service()"):
            assert banned not in src, f"{domain} internal routes must not call {banned}"
        assert "get_local_service" in src


def test_registry_key_namespace(monkeypatch):
    """多机隔离：有 GRAPE_GSB_NAMESPACE 时注册键为 service@namespace。"""
    from src.kernel.gsb.router import registry_key

    monkeypatch.setenv("GRAPE_GSB_NAMESPACE", "node-b")
    assert registry_key("mgmt") == "mgmt@node-b"
    assert registry_key("mgmt", "node-a") == "mgmt@node-a"
    monkeypatch.delenv("GRAPE_GSB_NAMESPACE", raising=False)
    assert registry_key("mgmt") == "mgmt"


def test_client_prefers_namespaced_registry_entry(monkeypatch):
    """客户端优先用本机命名空间的行，取不到再退回裸服务名（跨机/旧注册）。"""
    registry.register_service("runtime@node-b", "http://10.0.0.9:38083")
    monkeypatch.setenv("GRAPE_GSB_NAMESPACE", "node-b")
    client = GsbClient("runtime", caller_name="mgmt")
    assert client._registry_url() == "http://10.0.0.9:38083"

    monkeypatch.setenv("GRAPE_GSB_NAMESPACE", "node-a")
    client2 = GsbClient("runtime", caller_name="mgmt")
    # 本机命名空间没有行 → 多实例负载均衡下跨节点兜底（同一服务只有 node-b 一个实例）
    assert client2._registry_url() == "http://10.0.0.9:38083"


def test_gsb_client_metrics_installed_and_recorded():
    """GSB 客户端指标：安装到 insight registry 后 observe 能出现在 /metrics 文本里。"""
    from prometheus_client import CollectorRegistry, generate_latest

    from src.kernel.gsb import metrics as gsb_metrics

    reg = CollectorRegistry()
    # 重置模块级单例，模拟首次安装
    gsb_metrics._CLIENT_REQUESTS = None
    gsb_metrics._CLIENT_DURATION = None
    gsb_metrics.install(reg)
    gsb_metrics.observe("runtime", "mgmt", "GET", "200", 0.02)

    text = generate_latest(reg).decode()
    assert 'grape_gsb_client_requests_total{caller="mgmt",instance="",method="GET",status="200",target="runtime"} 1.0' in text
    assert "grape_gsb_client_duration_seconds_bucket" in text


def test_client_records_metric_on_success(monkeypatch):
    """RemoteProvider 走 GsbClient 时，每次调用都要落到流向指标（target/caller/status）。"""
    from src.kernel.gsb import metrics as gsb_metrics

    seen: list[tuple] = []
    monkeypatch.setattr(
        gsb_metrics,
        "observe",
        lambda **kw: seen.append((kw["target"], kw["caller"], kw["method"], str(kw["status"]))),
        raising=True,
    )

    registry.register_service("runtime", "http://alive-host:38083")

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class FakeHTTP:
        def request(self, method, url, headers=None, **kwargs):
            return FakeResponse()

    monkeypatch.setenv("GRAPE_RUNTIME_URL", "http://alive-host:38083")
    client = GsbClient("runtime", caller_name="mgmt")
    client._http = FakeHTTP()

    assert client.get("/internal/runtimes") == {"ok": True}
    assert seen == [("runtime", "mgmt", "GET", "200")]


def test_selftest_runs_probes_and_reports(monkeypatch):
    """GSB 自检：remote 模式执行一轮，两个只读探针都被调用且返回 ok。"""
    from src.kernel.gsb import selftest

    calls: list[str] = []

    class _Svc:
        def __init__(self, name):
            self.name = name

        def get_runtimes(self):
            calls.append("runtime.get_runtimes")
            return []

        def aggregate_runtime_stats(self):
            calls.append("insight.aggregate_runtime_stats")
            return {}

    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    monkeypatch.delenv("GRAPE_GSB_SELFTEST", raising=False)
    monkeypatch.setattr(selftest, "get_service", lambda name: _Svc(name), raising=True)

    assert selftest.selftest_enabled() is True
    result = selftest.run_selftest_once()
    assert result == {"runtime": "ok", "insight": "ok"}
    assert calls == ["runtime.get_runtimes", "insight.aggregate_runtime_stats"]


def test_selftest_disabled_when_local_or_off(monkeypatch):
    from src.kernel.gsb import selftest

    monkeypatch.delenv("GRAPE_PROVIDER_MODE", raising=False)
    assert selftest.selftest_enabled() is False

    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    monkeypatch.setenv("GRAPE_GSB_SELFTEST", "off")
    assert selftest.selftest_enabled() is False
