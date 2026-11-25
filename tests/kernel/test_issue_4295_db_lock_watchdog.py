"""Issue #4295：db_lock 持有中看门狗（Watchdog）单元测试。

慢持有日志过去只在 release **之后**才打（``_on_release_post``）：一次 99.9s 的持有，
日志要 100s 后才落地，且当时已无法还原"卡在哪一行"。看门狗在持有**期间**按周期扫
``snapshot()``，超阈值立即 ERROR + dump 持锁线程栈（``sys._current_frames()``），
并按 owner 去重。

硬约束：看门狗**绝不获取 db_lock**（只读 ``_STATE`` 快照），否则它自己会成为新的
等待者；同时它不能把持锁线程拖慢。
"""
from __future__ import annotations

import contextvars
import os
import select
import threading
import time

import pytest

from src.kernel import db_lock_metrics as dlm
from src.kernel.config import path_var, request_id_var


class _FakeLog:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg, *args, **kwargs):  # noqa: A003
        self.errors.append(msg % args if args else str(msg))

    def warning(self, msg, *args, **kwargs):  # noqa: A003
        self.warnings.append(msg % args if args else str(msg))


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    with dlm._WATCHDOG_REPORTED_LOCK:
        dlm._WATCHDOG_REPORTED.clear()
    path_var.set("")
    request_id_var.set("-")
    # 每个用例重置阈值/周期，避免互相污染
    monkeypatch.setenv(dlm.WATCHDOG_HOLD_ENV, "0.05")
    monkeypatch.setenv(dlm.WATCHDOG_DEDUP_ENV, "60")
    yield
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    with dlm._WATCHDOG_REPORTED_LOCK:
        dlm._WATCHDOG_REPORTED.clear()
    path_var.set("")
    request_id_var.set("-")


def _hold_lock(lock, hold_s: float, name: str = "bg-slow-holder"):
    """持锁线程在**拷贝的请求上下文**里跑：owner 标签取到请求路径（与生产同形态）。"""
    ready = threading.Event()
    release = threading.Event()

    def _holder():
        with lock:
            ready.set()
            release.wait(hold_s)

    ctx = contextvars.copy_context()
    t = threading.Thread(target=lambda: ctx.run(_holder), name=name, daemon=True)
    t.start()
    assert ready.wait(5)
    return t, release


def test_scan_reports_slow_holder_with_holder_thread_stack(monkeypatch):
    fake = _FakeLog()
    monkeypatch.setattr(dlm, "_log", lambda: fake)
    path_var.set("/api/issues/acme/demo")

    lock = dlm.TrackedRLock(bound_in_request=True)
    holder, release = _hold_lock(lock, 1.5)
    try:
        time.sleep(0.12)  # 越过 0.05s 阈值（看门狗不动锁，只读快照）
        hits = dlm.watchdog_scan()
    finally:
        release.set()
        holder.join(5)

    assert len(hits) == 1, f"应上报一次慢持有，实际 {hits}"
    hit = hits[0]
    assert hit["owner"].startswith("/api/issues/")
    assert hit["held_s"] >= 0.05
    assert hit["stack"], "必须带持锁线程栈"
    assert any("_holder" in line for line in hit["stack"]), f"栈里应能看到持有函数: {hit['stack']}"
    assert any("db_lock watchdog" in m and "holder_stack" in m for m in fake.errors)


def test_scan_dedupes_same_owner_within_window(monkeypatch):
    monkeypatch.setattr(dlm, "_log", lambda: _FakeLog())
    path_var.set("/api/issues/acme/demo")

    lock = dlm.TrackedRLock(bound_in_request=True)
    holder, release = _hold_lock(lock, 1.5)
    try:
        time.sleep(0.12)
        first = dlm.watchdog_scan()
        second = dlm.watchdog_scan()
    finally:
        release.set()
        holder.join(5)

    assert len(first) == 1
    assert second == [], "同 owner 在去重窗口内不得重复上报（防日志风暴）"


def test_scan_ignores_short_holds():
    lock = dlm.TrackedRLock(bound_in_request=True)
    with lock:
        assert dlm.watchdog_scan() == []


def test_scan_never_blocks_on_a_held_lock(monkeypatch):
    """看门狗不得获取 db_lock：持有者持锁期间扫描必须在毫秒级返回。"""
    monkeypatch.setattr(dlm, "_log", lambda: _FakeLog())
    path_var.set("/api/issues/acme/demo")

    lock = dlm.TrackedRLock(bound_in_request=True)
    holder, release = _hold_lock(lock, 1.0)
    try:
        time.sleep(0.12)
        t0 = time.perf_counter()
        dlm.watchdog_scan()
        elapsed = time.perf_counter() - t0
    finally:
        release.set()
        holder.join(5)

    assert elapsed < 0.3, f"看门狗阻塞了 {elapsed:.3f}s，说明它拿了锁"


def test_scan_reports_metric_per_owner(monkeypatch):
    from prometheus_client import CollectorRegistry

    registry = CollectorRegistry()
    dlm.install(registry)
    monkeypatch.setattr(dlm, "_log", lambda: _FakeLog())
    path_var.set("/api/scenes/with-pipeline")

    lock = dlm.TrackedRLock(bound_in_request=True)
    holder, release = _hold_lock(lock, 1.5)
    try:
        time.sleep(0.12)
        dlm.watchdog_scan()
    finally:
        release.set()
        holder.join(5)

    assert registry.get_sample_value(
        "grape_db_lock_watchdog_events_total",
        {"owner": "/api/scenes/with-pipeline"}) == 1.0


def test_watchdog_disabled_when_threshold_is_zero(monkeypatch):
    monkeypatch.setenv(dlm.WATCHDOG_HOLD_ENV, "0")
    lock = dlm.TrackedRLock(bound_in_request=True)
    holder, release = _hold_lock(lock, 0.3)
    try:
        time.sleep(0.12)
        assert dlm.watchdog_scan() == []
    finally:
        release.set()
        holder.join(5)


def test_watchdog_thread_starts_with_session_manager_and_is_idempotent():
    import src.kernel.session_manager  # noqa: F401 - 触发锁实体就位时的自动启动

    assert dlm.watchdog_running(), "session_manager 导入后看门狗应已在位"
    assert dlm.start_watchdog() is False, "重复启动必须幂等（不制造第二只看门狗）"


def test_stop_and_restart_watchdog(monkeypatch):
    monkeypatch.setenv(dlm.WATCHDOG_PERIOD_ENV, "0.05")
    monkeypatch.setenv(dlm.WATCHDOG_HOLD_ENV, "5")
    dlm.stop_watchdog()
    assert not dlm.watchdog_running()

    assert dlm.start_watchdog() is True
    assert dlm.watchdog_running()
    dlm.stop_watchdog()
    assert not dlm.watchdog_running()


def test_watchdog_thread_is_daemon(monkeypatch):
    monkeypatch.setenv(dlm.WATCHDOG_PERIOD_ENV, "0.05")
    dlm.stop_watchdog()
    assert dlm.start_watchdog() is True
    try:
        t = dlm._WATCHDOG_THREAD
        assert t is not None
        assert t.daemon is True, "看门狗不能阻止进程退出"
        assert t.name == "db_lock_watchdog"
    finally:
        dlm.stop_watchdog()
        monkeypatch.delenv(dlm.WATCHDOG_PERIOD_ENV, raising=False)
        dlm.start_watchdog()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="需要 POSIX fork")
def test_watchdog_restarts_in_fork_child_after_preload():
    """preload 形态（先 import 后 fork）：fork 子进程必须自己拉起看门狗并清空死状态。

    gunicorn ``--preload`` / multiprocessing fork 下，父进程 import 时启动的看门狗线程
    不跨 fork；若不在 ``os.register_at_fork(after_in_child=...)`` 里重启，worker 内
    ``watchdog_running()`` 恒为 False、``snapshot()`` 为空 → 看门狗静默失效。
    """
    import src.kernel.session_manager  # noqa: F401 - 父进程先按生产路径就位

    assert dlm.watchdog_running(), "父进程看门狗应在位（模拟 preload 主进程）"
    ghost_tid = 987654321  # 父进程/任意线程的持锁残留，子进程必须丢弃
    dlm._STATE[ghost_tid] = {"owner": "bg:ghost", "rid": "-", "tid": ghost_tid,
                            "since": time.perf_counter(), "depth": 1}

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # 子进程：只看两个事实，然后立即 _exit（不回流 pytest）
        try:
            running = dlm.watchdog_running()
            clean = dlm.snapshot() == []
            os.write(w, (b"1" if running else b"0") + (b"1" if clean else b"0"))
        finally:
            os._exit(0)

    os.close(w)
    payload = b""
    try:
        readable, _, _ = select.select([r], [], [], 10)
        payload = os.read(r, 2) if readable else b""
    finally:
        os.close(r)
        os.waitpid(pid, 0)

    assert payload == b"11", (
        f"子进程应重启看门狗并清空父进程持锁状态（第1位=watchdog_running，第2位=snapshot 为空），"
        f"实际 {payload!r}")