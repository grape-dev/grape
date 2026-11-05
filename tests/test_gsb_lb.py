"""GSB 多实例负载均衡单元测试（docs/weekly-09 §7.8.4 / 2026-11 增强）。

覆盖：
- registry.list_instances()：一次拿到 service / service@namespace 全部实例，过滤心跳过期
- GsbClient 策略：local_first / round_robin / random / least_inflight
- 实例冷却（transport 失败后短期跳过）与 env 静态钉住语义
"""
from __future__ import annotations

import time

import pytest

from src.kernel import session_manager
from src.kernel.gsb import registry
from src.kernel.gsb.client import GsbClient, reset_instance_cache, resolve_lb_strategy


def _reset() -> None:
    registry._ensure_table()
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute("DELETE FROM gsb_service_registry")
        session_manager.db.commit()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("GRAPE_MGMT_URL", raising=False)
    monkeypatch.delenv("GRAPE_RUNTIME_URL", raising=False)
    monkeypatch.delenv("GRAPE_GSB_NAMESPACE", raising=False)
    monkeypatch.delenv("GRAPE_GSB_LB", raising=False)
    from src.kernel.gsb import client as _client_mod

    _client_mod._RR_CURSOR.clear()
    reset_instance_cache()
    _reset()
    yield
    _client_mod._RR_CURSOR.clear()
    reset_instance_cache()
    _reset()


def _client(service="mgmt", namespace=None, strategy=None, monkeypatch=None):
    if namespace is not None:
        monkeypatch.setenv("GRAPE_GSB_NAMESPACE", namespace)
    if strategy is not None:
        monkeypatch.setenv("GRAPE_GSB_LB", strategy)
    reset_instance_cache()
    return GsbClient(service, caller_name="test")


# ---------------- registry.list_instances ----------------


def test_list_instances_returns_all_namespaces_with_metadata():
    registry.register_service("mgmt@hd002", "http://192.168.0.116:38081")
    registry.register_service("mgmt@vod", "http://192.168.4.245:38081")
    registry.register_service("mgmt", "http://127.0.0.1:38081")
    registry.register_service("runtime@hd002", "http://192.168.0.116:38083")

    rows = registry.list_instances("mgmt")
    assert {r["service_name"] for r in rows} == {"mgmt@hd002", "mgmt@vod", "mgmt"}
    assert {r["instance"] for r in rows} == {"hd002", "vod", ""}
    assert all(r["service"] == "mgmt" for r in rows)
    assert all(r["alive"] is True for r in rows)
    # 不串服务
    assert {r["service"] for r in registry.list_instances("runtime")} == {"runtime"}


def test_list_instances_excludes_stale_by_default():
    registry.register_service("mgmt@hd002", "http://192.168.0.116:38081")
    registry.register_service("mgmt@vod", "http://192.168.4.245:38081")
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "UPDATE gsb_service_registry SET last_heartbeat=? WHERE service_name=?",
            (time.time() - 500, "mgmt@vod"),
        )
        session_manager.db.commit()

    alive = registry.list_instances("mgmt")
    assert [r["instance"] for r in alive] == ["hd002"]

    all_rows = registry.list_instances("mgmt", include_offline=True)
    assert {r["instance"] for r in all_rows} == {"hd002", "vod"}
    stale = next(r for r in all_rows if r["instance"] == "vod")
    assert stale["alive"] is False


# ---------------- 策略解析 ----------------


def test_resolve_lb_strategy_defaults_and_aliases(monkeypatch):
    monkeypatch.delenv("GRAPE_GSB_LB", raising=False)
    assert resolve_lb_strategy() == "local_first"
    for raw, expect in (("rr", "round_robin"), ("least", "least_inflight"), ("random", "random")):
        monkeypatch.setenv("GRAPE_GSB_LB", raw)
        assert resolve_lb_strategy() == expect
    monkeypatch.setenv("GRAPE_GSB_LB", "nonsense")
    assert resolve_lb_strategy() == "local_first"


# ---------------- 策略排序 ----------------


def test_local_first_prefers_local_namespace(monkeypatch):
    registry.register_service("mgmt@hd002", "http://10.0.0.1:38081")
    registry.register_service("mgmt@vod", "http://10.0.0.2:38081")

    client = _client(namespace="hd002", monkeypatch=monkeypatch)
    assert client._candidates()[0] == "http://10.0.0.1:38081"

    client_vod = _client(namespace="vod", monkeypatch=monkeypatch)
    assert client_vod._candidates()[0] == "http://10.0.0.2:38081"


def test_round_robin_rotates_across_instances(monkeypatch):
    registry.register_service("mgmt@hd002", "http://10.0.0.1:38081")
    registry.register_service("mgmt@vod", "http://10.0.0.2:38081")

    client = _client(namespace="hd002", strategy="round_robin", monkeypatch=monkeypatch)
    firsts = []
    for _ in range(4):
        reset_instance_cache()
        firsts.append(client._candidates()[0])

    assert firsts == [
        "http://10.0.0.1:38081",
        "http://10.0.0.2:38081",
        "http://10.0.0.1:38081",
        "http://10.0.0.2:38081",
    ]


def test_random_returns_permutation(monkeypatch):
    registry.register_service("mgmt@hd002", "http://10.0.0.1:38081")
    registry.register_service("mgmt@vod", "http://10.0.0.2:38081")

    client = _client(strategy="random", monkeypatch=monkeypatch)
    candidates = client._candidates()
    # 随机只影响顺序，不丢实例；最后一个永远是本机回退
    assert set(candidates[:-1]) == {"http://10.0.0.1:38081", "http://10.0.0.2:38081"}
    assert candidates[-1] == "http://127.0.0.1:38081"


def test_least_inflight_prefers_idle_instance(monkeypatch):
    registry.register_service("mgmt@hd002", "http://10.0.0.1:38081")
    registry.register_service("mgmt@vod", "http://10.0.0.2:38081")

    client = _client(strategy="least_inflight", monkeypatch=monkeypatch)
    client._inflight["10.0.0.1:38081"] = 5
    client._inflight["10.0.0.2:38081"] = 0
    assert client._candidates()[0] == "http://10.0.0.2:38081"

    client._inflight["10.0.0.1:38081"] = 0
    client._inflight["10.0.0.2:38081"] = 3
    assert client._candidates()[0] == "http://10.0.0.1:38081"


def test_round_robin_cursor_shared_across_client_instances(monkeypatch):
    """RemoteProvider 每次调用都 new GsbClient，轮询游标必须进程级共享，否则永远选第一个。"""
    registry.register_service("mgmt@hd002", "http://10.0.0.1:38081")
    registry.register_service("mgmt@vod", "http://10.0.0.2:38081")
    monkeypatch.setenv("GRAPE_GSB_LB", "round_robin")

    firsts = []
    for _ in range(4):
        reset_instance_cache()
        fresh = GsbClient("mgmt", caller_name="test")  # 模拟 RemoteProvider 每次新建
        firsts.append(fresh._candidates()[0])
    assert firsts == [
        "http://10.0.0.1:38081",
        "http://10.0.0.2:38081",
        "http://10.0.0.1:38081",
        "http://10.0.0.2:38081",
    ]


# ---------------- 冷却与静态钉住 ----------------


def test_cooldown_skips_failed_instance(monkeypatch):
    registry.register_service("mgmt@hd002", "http://10.0.0.1:38081")
    registry.register_service("mgmt@vod", "http://10.0.0.2:38081")

    client = _client(namespace="hd002", strategy="round_robin", monkeypatch=monkeypatch)
    client._mark_failure("http://10.0.0.1:38081")
    reset_instance_cache()

    firsts = {client._candidates()[0] for _ in range(3)}
    assert firsts == {"http://10.0.0.2:38081"}

    # 冷却过期后重新参与
    client._cooldown_until["10.0.0.1:38081"] = time.time() - 1
    seen = set()
    for _ in range(4):
        reset_instance_cache()
        seen.add(client._candidates()[0])
    assert "http://10.0.0.1:38081" in seen


def test_env_url_still_pins_target(monkeypatch):
    registry.register_service("mgmt@hd002", "http://10.0.0.1:38081")
    registry.register_service("mgmt@vod", "http://10.0.0.2:38081")
    monkeypatch.setenv("GRAPE_MGMT_URL", "http://pinned:38081")

    client = _client(namespace="hd002", strategy="round_robin", monkeypatch=monkeypatch)
    assert client._candidates()[0] == "http://pinned:38081"


def test_no_instances_falls_back_to_localhost(monkeypatch):
    client = _client(service="insight", monkeypatch=monkeypatch)
    assert client._candidates() == ["http://127.0.0.1:38084"]