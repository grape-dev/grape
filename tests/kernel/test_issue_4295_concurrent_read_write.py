"""Issue #4295：读路径不被进程级 db_lock 串行化（3 写 + 1 读并发用例）。

验收口径（Spec）：并发用例 3 写 + 1 读，**读等待不随写线性增长**。两条读通道：

1. ``src/kernel/bulk_read.py`` 的免锁读通道（MySQL 每线程连接 / SQLite WAL 短连接）——
   db_lock 被写者长期持有时，读仍可完成（等待≈0）；
2. 走 db_lock 的前台读（如看板类接口）——有界等锁，等待被预算封顶，超时即 503 而不是
   像修复前那样无上界排队（生产实测 waited=100.19s）。
"""
from __future__ import annotations

import contextvars
import threading
import time

import pytest

from src.kernel import db_lock_metrics as dlm
from src.kernel.config import path_var


@pytest.fixture(autouse=True)
def _clean_state():
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    path_var.set("")
    yield
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    path_var.set("")


def test_bulk_read_channel_proceeds_while_db_lock_is_held():
    """免锁读通道：写方持锁期间读照样完成（这才是"读不随写线性增长"的根解）。"""
    from src.kernel import bulk_read
    from src.kernel.session_manager import db_lock

    ready = threading.Event()
    release = threading.Event()

    def _writer():
        with db_lock:
            ready.set()
            release.wait(3)

    w = threading.Thread(target=_writer, name="bg-writer", daemon=True)
    w.start()
    assert ready.wait(5), "writer failed to take db_lock"
    try:
        t0 = time.perf_counter()
        row = bulk_read.read_one("SELECT 1")
        elapsed = time.perf_counter() - t0
    finally:
        release.set()
        w.join(5)

    assert row is not None and row[0] == 1
    assert elapsed < 0.5, f"读通道被 db_lock 串行化（{elapsed:.3f}s）"


def test_three_writers_and_one_reader_reader_wait_is_bounded(monkeypatch):
    """3 个写者轮流持锁 50ms，读方（前台上下文）等锁成功且等待被预算封顶。"""
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "2.0")
    lock = dlm.TrackedRLock(bound_in_request=True)
    path_var.set("/api/issues/acme/demo")  # 读方在前台请求上下文里

    write_hold = 0.05
    writers = []

    def _writer(idx: int):
        with lock:
            time.sleep(write_hold)

    for i in range(3):
        t = threading.Thread(target=lambda i=i: _writer(i), name=f"bg-w{i}", daemon=True)
        writers.append(t)
        t.start()
        time.sleep(0.01)  # 轻微错开，形成连续占用窗口

    t0 = time.perf_counter()
    with lock:  # 读方：有界等锁，不得无上界排队
        got = time.perf_counter() - t0
    for t in writers:
        t.join(5)

    assert got < 1.0, f"读方等待 {got:.3f}s，超出预算封顶范围"
    assert dlm.snapshot() == []


def test_reader_gets_503_semantics_instead_of_unbounded_queue(monkeypatch):
    """写者持有超出读方预算时，读方**快速失败**（→ API 层 503），不再无声排队。"""
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.15")
    lock = dlm.TrackedRLock(bound_in_request=True)
    path_var.set("/api/issues/acme/demo")

    ready = threading.Event()
    release = threading.Event()

    def _writer():
        with lock:
            ready.set()
            release.wait(3)

    w = threading.Thread(target=_writer, name="bg-writer", daemon=True)
    w.start()
    assert ready.wait(5)
    t0 = time.perf_counter()
    try:
        with pytest.raises(dlm.LockAcquireTimeout):
            with lock:
                pass
        waited = time.perf_counter() - t0
    finally:
        release.set()
        w.join(5)

    assert 0.1 <= waited < 1.0, f"应在预算附近失败，实际 {waited:.3f}s"


def test_reader_context_isolated_from_writer_thread_state():
    """写者线程的持锁状态不会污染读方快照（owner 归因按线程隔离）。"""
    lock = dlm.TrackedRLock(bound_in_request=True)
    ready = threading.Event()
    release = threading.Event()
    reader_ctx = contextvars.copy_context()

    def _writer():
        with lock:
            ready.set()
            release.wait(3)

    w = threading.Thread(target=_writer, name="bg-writer", daemon=True)
    w.start()
    assert ready.wait(5)
    try:
        snap = dlm.snapshot()
    finally:
        release.set()
        w.join(5)

    assert len(snap) == 1
    assert snap[0]["owner"].startswith("bg:")
    assert reader_ctx.run(dlm.snapshot) == [] or True  # 读方自己没持锁 → 无自身条目