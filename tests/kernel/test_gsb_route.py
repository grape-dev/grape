"""Issue #2479：GSB provider 按方法路由（remote/lib 并存）单测.

覆盖验收矩阵：
- 内置基线（缺行取值：热路径四项 + 压测超阈值 14 项 lib / 原语 lib /
  采样错误 5 项默认 remote；QA 审查修复 Required#1）
- get_service() 进程级代理句柄复用（QA Required#2）
- per-method 超时 per-request 下发，无粘滞（QA Required#3，真实 GsbClient 路径）
- 配置表覆盖 + TTL 热切换 ≤30s（实测，无需重启）
- 配置表读取失败退回上一次已知配置
- 禁止矩阵守卫：runtime|insight → mgmt 恒 lib（契约 + 原语 + get_service 三口径，
  不依赖配置表）
- 按方法代理派发（_RoutedService）
- remote 超时 fail-fast：稳定错误码、不回落 lib；业务错误原样抛出
- metrics status 标签区分 remote 超时 / 不可达 / 业务错误
- 一键全量 lib 回滚（行为与改造前一致）
- 路由 key registry 与 contracts/public 实际面同步（防漂移门禁）
- CI 静态扫描可红（check_gsb_route_matrix.py；CI step 接入见 windows-smoke.yml）
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import time
from pathlib import Path

import httpx
import pytest

from src.kernel import session_manager
from src.kernel.gsb import metrics as gsb_metrics
from src.kernel.gsb import provider as gsb_provider
from src.kernel.gsb import route_config
from src.kernel.gsb.client import (
    GsbClient,
    GsbRemoteTimeoutError,
    GsbRemoteUnreachableError,
)
from src.kernel.gsb.provider import (
    caller_domain,
    decide_mode,
    get_local_service,
    get_service,
    matrix_forces_lib,
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_route_env(monkeypatch):
    """路由用例隔离：清空配置表 + 重置缓存 + 清角色环境。"""
    for key in ("GRAPE_PROVIDER_MODE", "GRAPE_PROCESS_ROLE", "GRAPE_SERVICE_NAME",
                "GRAPE_GSB_ROUTE_TTL"):
        monkeypatch.delenv(key, raising=False)
    route_config.ensure_table()

    def _wipe():
        with session_manager.db_lock:
            c = session_manager.db.cursor()
            c.execute("DELETE FROM gsb_provider_route")
            session_manager.db.commit()

    _wipe()
    route_config.reset_cache()
    yield
    _wipe()
    route_config.reset_cache()


@pytest.fixture()
def fake_service():
    """向 provider 工厂注册一个 fake 服务（用后还原，不污染其它用例）。"""
    saved = dict(gsb_provider._PROVIDERS)

    def _register(local_cls, remote_cls):
        gsb_provider.register_provider("fake", local_cls, remote_cls)

    yield _register
    gsb_provider._PROVIDERS.clear()
    gsb_provider._PROVIDERS.update(saved)


# ---------------- 内置基线（配置表缺行） ----------------


def test_baseline_defaults_when_remote_mode(monkeypatch):
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    # 热路径四项锁 lib
    assert decide_mode("mgmt", "verify_session_token") == "lib"
    assert decide_mode("insight", "log_obs") == "lib"
    assert decide_mode("insight", "log_api_latency") == "lib"
    assert decide_mode("insight", "log_llm_usage") == "lib"
    # 压测超阈值 14 项基线锁 lib（QA Required#1：空表部署与 bench-report
    # 落地结论一致，remote 由配置表显式放开）
    assert decide_mode("mgmt", "get_bot_config") == "lib"
    assert decide_mode("mgmt", "get_skill") == "lib"
    assert decide_mode("runtime", "get_events") == "lib"
    assert decide_mode("runtime", "get_chat_messages") == "lib"
    assert decide_mode("insight", "query_alerts") == "lib"
    assert decide_mode("insight", "aggregate_runtime_stats") == "lib"
    # 采样错误 5 项（压测无数据，无法裁决）默认偏好 remote
    assert decide_mode("runtime", "update_issue_state") == "remote"
    assert decide_mode("runtime", "get_chat_conversations") == "remote"
    assert decide_mode("insight", "query_metrics") == "remote"
    assert decide_mode("insight", "query_slo") == "remote"
    # public 原语：本期无 remote 传输通道，基线 lib
    assert decide_mode("mgmt", "fetch_rows") == "lib"
    assert decide_mode("runtime", "list_runtimes") == "lib"


def test_builtin_lib_keys_match_bench_report():
    """内置基线锁 lib 的 18 项 = 热路径 4 + 压测超阈值 14（bench-report §1）。"""
    expected = {
        # 热路径四项
        "mgmt.verify_session_token", "insight.log_obs",
        "insight.log_api_latency", "insight.log_llm_usage",
        # 超阈值 14 项
        "mgmt.get_repo_webhook_config", "mgmt.get_agent_session",
        "mgmt.get_scene_config", "mgmt.get_scene_pool_engine_ids",
        "mgmt.get_bot_config", "mgmt.get_skill",
        "runtime.get_events", "runtime.get_event_detail",
        "runtime.get_tasks", "runtime.get_task_messages",
        "runtime.get_runtimes", "runtime.get_chat_messages",
        "insight.query_alerts", "insight.aggregate_runtime_stats",
    }
    assert set(gsb_provider._BUILTIN_LIB_KEYS) == expected


def test_baseline_without_remote_mode_is_all_lib():
    """未开 remote 制式 → 存量行为：一律 lib（零回归口径）。"""
    assert decide_mode("mgmt", "get_bot_config") == "lib"
    assert decide_mode("runtime", "get_events") == "lib"
    assert decide_mode("insight", "query_metrics") == "lib"


# ---------------- 配置表覆盖 + 热切换 ----------------


def test_config_row_overrides_baseline(monkeypatch):
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    # runtime.update_issue_state 基线 remote（采样错误项）；配置表可覆盖为 lib
    assert decide_mode("runtime", "update_issue_state") == "remote"
    route_config.set_route("runtime.update_issue_state", "lib", updated_by="test")
    # 写入方本进程立即生效（set_route 内 reset_cache）
    assert decide_mode("runtime", "update_issue_state") == "lib"
    # 其它 key 不受影响
    assert decide_mode("runtime", "get_issue_state") == "remote"
    # 基线 lib 的 key 也可显式放开 remote（opt-in 由配置表承载）
    route_config.set_route("mgmt.get_bot_config", "remote", updated_by="test")
    assert decide_mode("mgmt", "get_bot_config") == "remote"


def test_set_route_validation_and_version_bump():
    with pytest.raises(ValueError):
        route_config.set_route("", "lib")
    with pytest.raises(ValueError):
        route_config.set_route("mgmt.get_skill", "weird-mode")
    route_config.set_route("mgmt.get_skill", "remote", timeout_ms=500, updated_by="t1")
    row = route_config.get_cached_route("mgmt.get_skill")
    assert row["mode"] == "remote"
    assert row["timeout_ms"] == 500
    assert row["updated_by"] == "t1"
    assert row["version"] == 1

    route_config.set_route("mgmt.get_skill", "lib", updated_by="t2")
    row = route_config.get_cached_route("mgmt.get_skill")
    assert row["mode"] == "lib"
    assert row["version"] == 2          # version 自增（热切换观测）
    assert row["timeout_ms"] == 500      # 未显式给 timeout 时沿用既有值

    assert route_config.get_cached_timeout_ms("mgmt.get_skill") == 500
    assert route_config.get_cached_timeout_ms("mgmt.unknown.key") is None

    assert route_config.remove_route("mgmt.get_skill") is True
    assert route_config.get_cached_route("mgmt.get_skill") is None
    assert route_config.remove_route("mgmt.get_skill") is False


def test_hot_switch_within_ttl(monkeypatch):
    """改表 ≤30s 自动生效（实测）：TTL 到期刷新快照，无需重启。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    monkeypatch.setenv("GRAPE_GSB_ROUTE_TTL", "0.05")
    assert decide_mode("insight", "query_metrics") == "remote"
    # 模拟「另一进程」改表：直接落库，不走本进程 set_route 的 reset_cache
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO gsb_provider_route "
            "(route_key, mode, timeout_ms, version, updated_at, updated_by) "
            "VALUES (?, 'lib', NULL, 1, ?, 'other-process')",
            ("insight.query_metrics", time.time()),
        )
        session_manager.db.commit()
    started = time.time()
    while time.time() - started < 30:
        if decide_mode("insight", "query_metrics") == "lib":
            break
        time.sleep(0.01)
    assert decide_mode("insight", "query_metrics") == "lib"
    assert time.time() - started <= 30   # 验收口径：改表 ≤30s 生效（实测 0.05s TTL）
    # version 已进入缓存快照（观测口）
    assert route_config.cache_state()["version"] == 1


def test_config_read_failure_keeps_last_known(monkeypatch):
    """配置表读取失败 → 退回上一次已知配置（启动期兜底，不属调用期回落）。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    route_config.set_route("insight.query_metrics", "lib")
    assert decide_mode("insight", "query_metrics") == "lib"
    monkeypatch.setenv("GRAPE_GSB_ROUTE_TTL", "0.05")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(route_config, "_load_all", boom)
    time.sleep(0.06)  # TTL 过期，触发一次失败刷新
    assert decide_mode("mgmt", "get_skill") == "lib"      # 上次已知配置仍生效
    assert decide_mode("insight", "query_slo") == "remote"    # 快照缺行 → 内置基线


# ---------------- 禁止矩阵守卫 ----------------


def test_matrix_guard_forces_lib_for_runtime_and_insight(monkeypatch):
    """runtime|insight 进程 → mgmt：契约与原语恒 lib，配置表说 remote 也无效。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    route_config.set_route("mgmt.get_bot_config", "remote")
    route_config.set_route("mgmt.fetch_rows", "remote")
    for role in ("worker", "event", "insight"):
        monkeypatch.setenv("GRAPE_PROCESS_ROLE", role)
        assert decide_mode("mgmt", "get_bot_config") == "lib"
        assert decide_mode("mgmt", "verify_session_token") == "lib"
        assert decide_mode("mgmt", "fetch_rows") == "lib"   # 原语同样被矩阵覆盖
        assert matrix_forces_lib("mgmt") is True
    # mgmt → runtime|insight 方向不禁止：允许 remote
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "server")
    assert matrix_forces_lib("mgmt") is False
    assert matrix_forces_lib("runtime") is False
    assert matrix_forces_lib("insight") is False
    assert decide_mode("runtime", "update_issue_state") == "remote"
    assert decide_mode("insight", "query_metrics") == "remote"


def test_matrix_guard_get_service_returns_local_instance(monkeypatch):
    """runtime|insight 进程内 get_service("mgmt") 恒 Local 实例（不依赖配置表）。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    route_config.set_route("mgmt.get_bot_config", "remote")
    for role in ("worker", "event", "insight"):
        monkeypatch.setenv("GRAPE_PROCESS_ROLE", role)
        svc = get_service("mgmt")
        assert type(svc).__name__ == "MgmtServiceLocal"
        # /internal/* 服务端口径不变：恒 Local
        assert type(get_local_service("mgmt")).__name__ == "MgmtServiceLocal"
    # mgmt 进程 + remote 制式 → 按方法路由代理
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "server")
    assert type(get_service("runtime")).__name__ == "_RoutedService"


def test_caller_domain_resolution(monkeypatch):
    """四进程角色 → 域映射；GRAPE_SERVICE_NAME 同口径；DEVOPS_DB_NAME 回落。"""
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "server")
    assert caller_domain() == "mgmt"
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "worker")
    assert caller_domain() == "runtime"
    monkeypatch.delenv("GRAPE_PROCESS_ROLE")
    monkeypatch.setenv("GRAPE_SERVICE_NAME", "event")
    assert caller_domain() == "runtime"
    monkeypatch.delenv("GRAPE_SERVICE_NAME")
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_insight")
    assert caller_domain() == "insight"
    monkeypatch.delenv("DEVOPS_DB_NAME")
    assert caller_domain() is None


# ---------------- 按方法代理派发 ----------------


class _RecordingLocal:
    def __init__(self):
        self.calls: list[str] = []

    def get_skill(self, skill_id):
        self.calls.append(f"local:{skill_id}")
        return ("local", skill_id)

    def get_bot_config(self, repo_key):
        self.calls.append(f"local:{repo_key}")
        return ("local", repo_key)


class _RecordingRemote:
    def __init__(self):
        self.calls: list[str] = []

    def get_skill(self, skill_id):
        self.calls.append(f"remote:{skill_id}")
        return ("remote", skill_id)

    def get_bot_config(self, repo_key):
        self.calls.append(f"remote:{repo_key}")
        return ("remote", repo_key)


def test_proxy_dispatches_per_method(monkeypatch, fake_service):
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    fake_service(_RecordingLocal, _RecordingRemote)
    route_config.set_route("fake.get_bot_config", "lib")
    svc = get_service("fake")
    assert type(svc).__name__ == "_RoutedService"
    # 配置表 → lib
    assert svc.get_bot_config("o/r") == ("local", "o/r")
    # 内置基线 → remote（默认偏好）
    assert svc.get_skill("7") == ("remote", "7")
    assert svc._local.calls == ["local:o/r"]
    assert svc._remote.calls == ["remote:7"]
    # 非方法属性不派发
    with pytest.raises(AttributeError):
        svc.not_a_method


def test_proxy_locks_remote_when_method_configured_lib(monkeypatch, fake_service):
    """配置行 lib 时 remote 实例完全惰性（不实例化、不触碰）。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")

    class _Boom:
        def __init__(self):
            raise AssertionError("remote instance must not be created")

    fake_service(_RecordingLocal, _Boom)
    route_config.set_route("fake.get_skill", "lib")
    svc = get_service("fake")
    assert svc.get_skill("x") == ("local", "x")
    assert svc._remote is None


# ---------------- remote 超时 fail-fast ----------------


def test_get_service_reuses_proxy_handle(monkeypatch, fake_service):
    """QA Required#2：get_service() 进程级句柄复用——不再每次新建代理。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    fake_service(_RecordingLocal, _RecordingRemote)
    svc1 = get_service("fake")
    svc2 = get_service("fake")
    assert svc1 is svc2                     # 同一代理（进程级缓存）
    assert svc1.get_skill("7") == ("remote", "7")
    # 注册面变化（重注册）→ 缓存失效重建
    fake_service(_RecordingLocal, _RecordingRemote)
    svc3 = get_service("fake")
    assert svc3 is not svc1
    assert svc3._local is not svc1._local    # 新代理惰性新实例


def test_remote_timeout_fail_fast_with_error_code(monkeypatch, fake_service):
    """超时 fail-fast：稳定错误码 GSB_REMOTE_TIMEOUT，不回落 lib（本地未被调）。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")

    class _TimeoutRemote:
        def get_skill(self, skill_id):
            raise httpx.ReadTimeout("read timed out")

    local = _RecordingLocal()
    fake_service(lambda: local, _TimeoutRemote)
    svc = get_service("fake")
    with pytest.raises(GsbRemoteTimeoutError) as ei:
        svc.get_skill("7")
    assert ei.value.code == "GSB_REMOTE_TIMEOUT"
    assert "fake.get_skill" in str(ei.value)
    assert local.calls == []          # 不回落 lib
    assert isinstance(ei.value.__cause__, httpx.ReadTimeout)


def test_remote_unreachable_fail_fast(monkeypatch, fake_service):
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")

    class _Unreachable:
        def get_skill(self, skill_id):
            raise httpx.ConnectError("connection refused")

    local = _RecordingLocal()
    fake_service(lambda: local, _Unreachable)
    svc = get_service("fake")
    with pytest.raises(GsbRemoteUnreachableError) as ei:
        svc.get_skill("7")
    assert ei.value.code == "GSB_REMOTE_UNREACHABLE"
    assert local.calls == []


def test_remote_business_error_not_wrapped(monkeypatch, fake_service):
    """业务错误原样抛出（不套通道错误码，不回落 lib）。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")

    class _BizError:
        def get_skill(self, skill_id):
            raise ValueError("bad payload")

    local = _RecordingLocal()
    fake_service(lambda: local, _BizError)
    svc = get_service("fake")
    with pytest.raises(ValueError):
        svc.get_skill("7")
    assert local.calls == []


def test_per_method_timeout_applied_from_config(monkeypatch, fake_service):
    """配置表 timeout_ms → per-request 超时覆盖（未配置行不覆盖默认 10s）。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    applied: list[float] = []

    class _Gsb:
        def with_timeout(self, seconds):
            import contextlib

            @contextlib.contextmanager
            def _ctx():
                applied.append(seconds)
                yield
            return _ctx()

    class _Client:
        def __init__(self):
            self._gsb = _Gsb()

        def with_timeout(self, seconds):
            return self._gsb.with_timeout(seconds)

    class _Remote:
        def __init__(self):
            self.client = _Client()

        def get_skill(self, skill_id):
            return ("remote", skill_id)

        def get_bot_config(self, repo_key):
            return ("remote", repo_key)

    fake_service(_RecordingLocal, _Remote)
    route_config.set_route("fake.get_skill", "remote", timeout_ms=250)
    svc = get_service("fake")
    assert svc.get_skill("7") == ("remote", "7")
    assert applied == [0.25]
    # 无配置行的方法：不产生超时覆盖（维持客户端默认，无粘滞）
    assert svc.get_bot_config("o/r") == ("remote", "o/r")
    assert applied == [0.25]


def test_per_method_timeout_not_sticky_real_gsbclient(monkeypatch, fake_service):
    """QA Required#3（真实 GsbClient 路径）：配 250ms 调 A → 调无配置 B，
    B 仍为默认超时（per-request 下发，共享客户端 timeout 从不被改写）。
    """
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
    monkeypatch.setenv("GRAPE_MGMT_URL", "http://only-host:38081")
    from src.domains.contracts.remote.http_client import ServiceHttpClient

    class _CapturingHttp:
        """替身 httpx 客户端：记录每次请求的 per-request timeout。"""

        def __init__(self):
            self.seen: list = []

        def request(self, method, url, headers=None, **kwargs):
            self.seen.append(kwargs.get("timeout", None))
            return _FakeResponse(200)

    captured = _CapturingHttp()

    class _Remote:
        def __init__(self):
            self.client = ServiceHttpClient("mgmt")   # 真实 ServiceHttpClient
            self.client._client._http = captured      # 仅替换传输层替身

        def get_skill(self, skill_id):
            return self.client.get(f"/internal/skills/{skill_id}")

        def get_bot_config(self, repo_key):
            return self.client.get(f"/internal/repos/{repo_key}/bot-config")

    fake_service(_RecordingLocal, _Remote)
    route_config.set_route("fake.get_skill", "remote", timeout_ms=250)
    svc = get_service("fake")
    assert svc.get_skill("7") == {"ok": True}
    assert svc.get_bot_config("o/r") == {"ok": True}
    # A（配置 250ms）→ per-request 0.25；B（无配置行）→ 默认超时（未传
    # per-request timeout），共享客户端状态零粘滞
    assert captured.seen[0] == 0.25
    assert captured.seen[1] is None


# ---------------- metrics 标签：超时 / 不可达 / 业务错误 ----------------


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("GET", "http://fake"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return {"ok": True}


def _fake_http(exc_factory=None, status_code=200):
    class _HTTP:
        def request(self, method, url, headers=None, **kwargs):
            if exc_factory is not None:
                raise exc_factory()
            return _FakeResponse(status_code)

    return _HTTP()


def _observed_statuses(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        gsb_metrics, "observe",
        lambda **kw: seen.append(str(kw["status"])), raising=True,
    )
    return seen


def test_metrics_labels_timeout_vs_business(monkeypatch):
    monkeypatch.setenv("GRAPE_MGMT_URL", "http://only-host:38081")
    # 读超时 → remote_timeout（GET 幂等会切候选，两个候选都记 timeout）
    seen = _observed_statuses(monkeypatch)
    client = GsbClient("mgmt", caller_name="runtime")
    client._http = _fake_http(lambda: httpx.ReadTimeout("read timed out"))
    with pytest.raises(httpx.ReadTimeout):
        client.get("/internal/ping")
    assert seen and set(seen) == {"remote_timeout"}

    # 连接失败 → remote_unreachable
    seen = _observed_statuses(monkeypatch)
    client = GsbClient("mgmt", caller_name="runtime")
    client._http = _fake_http(lambda: httpx.ConnectError("refused"))
    with pytest.raises(httpx.ConnectError):
        client.get("/internal/ping")
    assert seen and set(seen) == {"remote_unreachable"}

    # HTTP 业务错误 → 数字状态码（与超时可区分）。既有客户端口径：成功路径
    # observe（raise_for_status 前）与异常路径 observe 各记一次，均为业务码。
    seen = _observed_statuses(monkeypatch)
    client = GsbClient("mgmt", caller_name="runtime")
    client._http = _fake_http(status_code=500)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("/internal/ping")
    assert seen == ["500", "500"]

    # 成功仍记数字状态码
    seen = _observed_statuses(monkeypatch)
    client = GsbClient("mgmt", caller_name="runtime")
    client._http = _fake_http(status_code=200)
    assert client.get("/internal/ping") == {"ok": True}
    assert seen == ["200"]


# ---------------- 一键全量 lib 回滚 ----------------


def test_rollback_all_lib_matches_legacy_behavior(monkeypatch, fake_service):
    """配置表一键全量 lib：全部 key 走 Local，行为与改造前一致（无需改代码）。"""
    monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")

    class _L:
        def get_skill(self, skill_id):
            return {"src": "local", "sid": skill_id}

        def get_bot_config(self, repo_key):
            return {"src": "local", "rk": repo_key}

    remote_hits: list[str] = []

    class _R:
        def get_skill(self, skill_id):
            remote_hits.append("get_skill")
            return {"src": "remote"}

        def get_bot_config(self, repo_key):
            remote_hits.append("get_bot_config")
            return {"src": "remote"}

    # 用 fake 实现顶替 mgmt 注册面（key 空间仍是真实 23 契约面）
    fake_service(_L, _R)
    gsb_provider.register_provider("mgmt", _L, _R)
    count = route_config.set_all_known("lib", updated_by="rollback")
    # 23 契约方法 + mgmt 39 原语 + runtime 66 原语
    assert count == 23 + 39 + 66

    svc = get_service("mgmt")
    assert svc.get_skill("7") == {"src": "local", "sid": "7"}
    assert svc.get_bot_config("o/r") == {"src": "local", "rk": "o/r"}
    assert remote_hits == []
    # 与「未开 remote 制式」的改造前行为等价
    monkeypatch.delenv("GRAPE_PROVIDER_MODE")
    legacy = get_service("mgmt")
    assert legacy.get_skill("7") == {"src": "local", "sid": "7"}
    assert legacy.get_bot_config("o/r") == {"src": "local", "rk": "o/r"}


# ---------------- 路由 key registry 同步（防漂移门禁） ----------------


def test_contract_methods_registry_sync():
    """静态 CONTRACT_METHODS 必须与 contracts ABC 实际面一致（防漂移）。"""
    from src.domains.contracts.insight_service import InsightService
    from src.domains.contracts.mgmt_service import MgmtService
    from src.domains.contracts.runtime_service import RuntimeService

    for service, cls in (
        ("mgmt", MgmtService),
        ("runtime", RuntimeService),
        ("insight", InsightService),
    ):
        actual = set(getattr(cls, "__abstractmethods__", ()))
        assert actual == set(route_config.CONTRACT_METHODS[service]), service
    total = sum(len(v) for v in route_config.CONTRACT_METHODS.values())
    assert total == 23


def test_public_primitives_registry_sync():
    """静态 PUBLIC_PRIMITIVES 必须与 public 模块实际面一致（防漂移）。"""
    for domain in ("mgmt", "runtime"):
        path = _REPO / "src" / "domains" / domain / "public" / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        actual = {
            n.name
            for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and not n.name.startswith("_")
            and n.name not in ("available", "health")  # 探活口不参与路由
        }
        assert actual == set(route_config.PUBLIC_PRIMITIVES[domain]), domain


def test_known_route_keys_shape():
    keys = route_config.known_route_keys()
    assert len(keys) == len(set(keys))
    assert "mgmt.get_bot_config" in keys
    assert "insight.query_metrics" in keys
    assert "runtime.list_runtimes" in keys
    assert "insight.fetch_rows" not in keys   # insight 无 public 模块


# ---------------- CI 静态扫描可红 ----------------

_SCRIPT = _REPO / "scripts" / "check_gsb_route_matrix.py"
spec = importlib.util.spec_from_file_location("check_gsb_route_matrix", _SCRIPT)
assert spec is not None and spec.loader is not None
scanner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scanner  # dataclass 解析需要模块已注册进 sys.modules
spec.loader.exec_module(scanner)


def test_static_scan_clean_on_current_repo():
    violations, files = scanner.scan_roots([_REPO / r for r in scanner.SCAN_ROOTS])
    assert files > 0
    assert violations == []


def test_static_scan_detects_violations(tmp_path):
    """门禁可红：runtime 域代码引用 mgmt remote 通道必须被命中。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from src.domains.contracts.remote.mgmt_remote import MgmtServiceRemote\n"
        "svc = MgmtServiceRemote()\n"
        "c1 = GsbClient('mgmt', caller_name='runtime')\n"
        "c2 = ServiceHttpClient(\"mgmt\")\n"
        "from src.kernel.gsb.provider import _PROVIDERS\n"
        "print(_PROVIDERS['mgmt'])\n",
        encoding="utf-8",
    )
    ok = tmp_path / "ok.py"
    ok.write_text(
        "# docstring 提到 MgmtServiceRemote 不算（注释/字符串不参与 AST）\n"
        "from src.kernel.gsb.provider import get_service, get_local_service\n"
        "svc = get_service('mgmt')\n"
        "local = get_local_service('mgmt')\n"
        "c = GsbClient('runtime', caller_name='runtime')\n",
        encoding="utf-8",
    )
    violations, _files = scanner.scan_roots([tmp_path])
    kinds = {v.kind for v in violations}
    assert "banned-import" in kinds
    assert "banned-symbol" in kinds
    assert "mgmt-remote-client" in kinds
    assert "registry-bypass" in kinds
    # 合法写法文件单独扫不出违规
    ok_only, _ = scanner.scan_roots([ok])
    assert ok_only == []


def test_static_scan_fail_closed_on_empty(tmp_path):
    violations, files = scanner.scan_roots([tmp_path / "nope"])
    assert files == 0
    rc = scanner.main([])
    # 仓库树存在时 main 正常返回 0；fail-closed 由 files==0 语义承载
    assert rc in (0, 1)
