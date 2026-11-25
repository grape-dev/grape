"""Issue #4295：前台请求的有界等锁（db_lock 等锁预算）单元测试。

背景：``db_lock`` 是进程级单把 RLock，``TrackedRLock.__enter__`` 过去调
``acquire()`` **不带 timeout**，等锁无上界（生产实测 ``waited=100.19s``）。修复后：

- **前台请求**（``path_var`` 非空）等锁受 ``DB_LOCK_ACQUIRE_TIMEOUT_S`` 约束
  （默认 5s，0=关闭→旧的无界语义）；预算耗尽抛 ``LockAcquireTimeout``，
  抛点在进入临界区**之前**，因此不会出现"没拿到锁却执行了 DB 写"。
- **后台线程/批处理**（无请求上下文）保持无界等锁，语义不变。
- **显式** ``acquire(timeout=N)`` 语义不变：等不到返回 ``False``（``pipeline_config`` 依赖）。
- **同线程重入**永不超时（RLock 可重入）。
"""
from __future__ import annotations

import threading
import time

import pytest

from src.kernel import db_lock_metrics as dlm
from src.kernel.config import path_var, request_id_var


class _FakeLog:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def error(self, msg, *args, **kwargs):  # noqa: A003 - 模拟 logger.error
        self.messages.append(msg % args if args else str(msg))

    def warning(self, msg, *args, **kwargs):  # noqa: A003
        self.messages.append(msg % args if args else str(msg))


@pytest.fixture(autouse=True)
def _clean_state():
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    dlm._BUDGET_OVERRIDE.set(0.0)
    with dlm._TIMEOUT_REPORTED_LOCK:
        dlm._TIMEOUT_REPORTED.clear()
    path_var.set("")
    request_id_var.set("-")
    yield
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    dlm._BUDGET_OVERRIDE.set(0.0)
    with dlm._TIMEOUT_REPORTED_LOCK:
        dlm._TIMEOUT_REPORTED.clear()
    path_var.set("")
    request_id_var.set("-")


def _hold_lock(lock, hold_s: float):
    """后台线程持锁 hold_s 秒；返回 (thread, ready_event)。"""
    ready = threading.Event()

    def _holder():
        with lock:
            ready.set()
            time.sleep(hold_s)

    t = threading.Thread(target=_holder, name="bg-holder", daemon=True)
    t.start()
    assert ready.wait(5), "holder failed to take the lock"
    return t


def test_budget_only_applies_to_foreground_request(monkeypatch):
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "5")
    path_var.set("")
    assert dlm.acquire_budget_s() == 0.0, "后台线程必须保持无界等锁"

    path_var.set("/api/issues/acme/demo")
    assert dlm.acquire_budget_s() == pytest.approx(5.0)


def test_budget_zero_disables_bounded_wait(monkeypatch):
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0")
    path_var.set("/api/issues/acme/demo")
    assert dlm.acquire_budget_s() == 0.0


def test_foreground_wait_over_budget_raises_and_does_not_enter_critical_section(monkeypatch):
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.15")
    lock = dlm.TrackedRLock(bound_in_request=True)
    holder = _hold_lock(lock, 1.5)
    path_var.set("/api/issues/acme/demo")
    entered: list[str] = []
    t0 = time.perf_counter()
    try:
        with pytest.raises(dlm.LockAcquireTimeout) as ei:
            with lock:
                entered.append("body-ran")
        waited = time.perf_counter() - t0
    finally:
        holder.join(5)

    assert entered == [], "预算耗尽时不得执行临界区（否则等于未加锁的 DB 访问）"
    assert 0.1 <= waited < 1.2, f"应在预算附近失败，实际 waited={waited:.3f}s"
    assert ei.value.timeout == pytest.approx(0.15)
    assert ei.value.owner.startswith("/api/issues/")
    assert ei.value.holder != "-", "超时信息必须带当前持有者，便于当场定位"
    assert elapsed_state_clean()


def elapsed_state_clean() -> bool:
    """超时路径不留持锁状态（没有 depth 残留）。"""
    return dlm.snapshot() == []


def test_background_thread_wait_is_still_unbounded(monkeypatch):
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.05")
    lock = dlm.TrackedRLock(bound_in_request=True)
    holder = _hold_lock(lock, 0.2)
    path_var.set("")  # 无请求上下文 = 后台路径
    t0 = time.perf_counter()
    with lock:
        pass
    waited = time.perf_counter() - t0
    holder.join(5)
    assert waited >= 0.1, "后台线程应继续等待（不受前台预算约束）"
    assert dlm.snapshot() == []


def test_explicit_timeout_still_returns_false(monkeypatch):
    """显式 timeout 的 RLock 契约不变（pipeline_config 的 acquire(timeout=30) 依赖）。"""
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.1")
    lock = dlm.TrackedRLock(bound_in_request=True)
    holder = _hold_lock(lock, 0.6)
    path_var.set("/api/issues/acme/demo")
    assert lock.acquire(timeout=0.05) is False
    holder.join(5)


def test_reentrant_acquisition_never_times_out(monkeypatch):
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.05")
    path_var.set("/api/issues/acme/demo")
    lock = dlm.TrackedRLock(bound_in_request=True)
    with lock:
        with lock:  # RLock 重入：立即成功，预算不参与
            assert len(dlm.snapshot()) == 1
            assert dlm.snapshot()[0]["depth"] == 2
    assert dlm.snapshot() == []


def test_foreground_lock_budget_override_without_request_path(monkeypatch):
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0")
    path_var.set("")
    with dlm.foreground_lock_budget(0.25):
        assert dlm.acquire_budget_s() == pytest.approx(0.25)
    assert dlm.acquire_budget_s() == 0.0


def test_timeout_raises_error_log_and_metric(monkeypatch):
    from prometheus_client import CollectorRegistry

    registry = CollectorRegistry()
    dlm.install(registry)
    fake = _FakeLog()
    monkeypatch.setattr(dlm, "_log", lambda: fake)
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.1")

    lock = dlm.TrackedRLock(bound_in_request=True)
    holder = _hold_lock(lock, 1.0)
    path_var.set("/api/issues/acme/demo")
    try:
        with pytest.raises(dlm.LockAcquireTimeout):
            with lock:
                pass
    finally:
        holder.join(5)

    assert any("db_lock acquire timeout" in m for m in fake.messages)
    assert registry.get_sample_value(
        "grape_db_lock_acquire_timeouts_total", {"owner": "/api/issues/acme/demo"}) == 1.0


def test_request_id_is_carried_into_timeout_log(monkeypatch):
    fake = _FakeLog()
    monkeypatch.setattr(dlm, "_log", lambda: fake)
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.1")
    request_id_var.set("rid-4295")

    lock = dlm.TrackedRLock(bound_in_request=True)
    holder = _hold_lock(lock, 1.0)
    path_var.set("/api/issues/acme/demo")
    try:
        with pytest.raises(dlm.LockAcquireTimeout):
            with lock:
                pass
    finally:
        holder.join(5)
    assert any("rid-4295" in m for m in fake.messages)


def _timeout_once(lock, path: str = "/api/issues/acme/demo") -> None:
    """在前台请求上下文里等一次锁并断言超预算失败。"""
    path_var.set(path)
    with pytest.raises(dlm.LockAcquireTimeout):
        with lock:
            pass


def test_timeout_error_log_is_deduped_per_owner(monkeypatch):
    """拥塞窗口里同一 owner 反复超时：指标逐次累加、ERROR 只打一条（防日志风暴）。"""
    from prometheus_client import CollectorRegistry

    registry = CollectorRegistry()
    dlm.install(registry)
    fake = _FakeLog()
    monkeypatch.setattr(dlm, "_log", lambda: fake)
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.05")
    monkeypatch.setenv(dlm.TIMEOUT_LOG_DEDUP_ENV, "60")

    lock = dlm.TrackedRLock(bound_in_request=True)
    holder = _hold_lock(lock, 0.5)
    try:
        _timeout_once(lock)
        _timeout_once(lock)
    finally:
        holder.join(5)

    timeouts = [m for m in fake.messages if "db_lock acquire timeout" in m]
    assert len(timeouts) == 1, f"同一 owner 在去重窗口内只应打一条 ERROR：{timeouts}"
    assert registry.get_sample_value(
        "grape_db_lock_acquire_timeouts_total", {"owner": "/api/issues/acme/demo"}) == 2.0


def test_timeout_error_log_dedup_can_be_disabled(monkeypatch):
    """``DB_LOCK_TIMEOUT_LOG_DEDUP_S=0`` 关闭日志去重（排障时每次都要看）。"""
    fake = _FakeLog()
    monkeypatch.setattr(dlm, "_log", lambda: fake)
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.05")
    monkeypatch.setenv(dlm.TIMEOUT_LOG_DEDUP_ENV, "0")

    lock = dlm.TrackedRLock(bound_in_request=True)
    holder = _hold_lock(lock, 0.5)
    try:
        _timeout_once(lock)
        _timeout_once(lock)
    finally:
        holder.join(5)

    timeouts = [m for m in fake.messages if "db_lock acquire timeout" in m]
    assert len(timeouts) == 2, f"去重关闭后每次超时都应打 ERROR：{timeouts}"


def test_non_blocking_acquire_is_never_bounded(monkeypatch):
    """``acquire(blocking=False)`` 不能被套上 timeout（threading 会抛 ValueError）。"""
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.05")
    path_var.set("/api/issues/acme/demo")
    lock = dlm.TrackedRLock(bound_in_request=True)

    assert lock.acquire(blocking=False) is True
    lock.release()

    holder = _hold_lock(lock, 0.5)
    try:
        t0 = time.perf_counter()
        assert lock.acquire(blocking=False) is False
        assert time.perf_counter() - t0 < 0.3, "非阻塞获取应立即返回，不得等待预算"
    finally:
        holder.join(5)


def test_owner_domain_lock_keeps_unbounded_semantics(monkeypatch):
    """域级 owner_lock 不参与前台预算（只有全局 db_lock 有界，缩小改动面）。"""
    from src.kernel.owner_db import owner_lock

    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.05")
    path_var.set("/api/issues/acme/demo")
    domain_lock = owner_lock("runtime")
    assert domain_lock._bound_in_request is False

    ready = threading.Event()
    release = threading.Event()

    def _holder():
        with domain_lock:
            ready.set()
            release.wait(0.4)

    t = threading.Thread(target=_holder, name="bg-domain", daemon=True)
    t.start()
    assert ready.wait(5)
    with domain_lock:  # 等待而非超时
        pass
    t.join(5)
