"""Issue #4295：读路径不被进程级 db_lock 串行化（3 写 + 1 读并发用例）。

验收口径（Spec）：并发用例 3 写 + 1 读，**读等待不随写线性增长**。两条读通道：

1. ``src/kernel/bulk_read.py`` 的免锁读通道（MySQL 每线程连接 / SQLite WAL 短连接）——
   db_lock 被写者长期持有时，读仍可完成（等待≈0）；
2. 走 db_lock 的前台读（如看板类接口）——有界等锁，等待被预算封顶，超时即 503 而不是
   像修复前那样无上界排队（生产实测 waited=100.19s）。
"""
from __future__ import annotations

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


def _reader_entries(snap: list[dict], reader_tid: int) -> list[dict]:
    """快照里归因到读方线程的条目；非空即说明读方线程被串扰（护栏判据）。"""
    return [st for st in snap if st.get("tid") == reader_tid]


def test_reader_context_isolated_from_writer_thread_state():
    """写者的持锁状态按线程归因：读方线程在快照里没有自己的条目。"""
    lock = dlm.TrackedRLock(bound_in_request=True)
    ready = threading.Event()
    release = threading.Event()
    reader_tid = threading.get_ident()

    def _writer():
        with lock:
            ready.set()
            release.wait(3)

    w = threading.Thread(target=_writer, name="bg-writer", daemon=True)
    w.start()
    assert ready.wait(5)
    try:
        snap = dlm.snapshot()
        # snapshot() 是进程级全局快照：写者持锁期间必须恰好一条，且归因到写者线程
        assert len(snap) == 1, f"写者持锁期间快照应恰好一条，实际 {snap}"
        assert snap[0]["tid"] == w.ident, f"条目应归因到写者线程，实际 {snap[0]}"
        assert snap[0]["owner"].startswith("bg:"), f"后台写者 owner 应以 bg: 开头：{snap[0]}"
        assert not _reader_entries(snap, reader_tid), (
            f"读方未持锁却出现了自身条目（线程归因串扰）：{_reader_entries(snap, reader_tid)}")
    finally:
        release.set()
        w.join(5)

    assert dlm.snapshot() == [], "写者释放后快照应清空"
    # 读方自取自还：能看到自己的一条 depth=1，释放后不留残留
    with lock:
        mine = _reader_entries(dlm.snapshot(), reader_tid)
        assert len(mine) == 1 and mine[0]["depth"] == 1, f"读方持锁时应恰好一条自身条目：{mine}"
    assert dlm.snapshot() == [], "读方释放后不得残留自身条目"


def test_reader_isolation_assertion_can_fail():
    """反证：人为注入读方线程的残留条目时，隔离判据必须失败（护栏非恒真）。"""
    reader_tid = threading.get_ident()
    polluted = [{"owner": "bg:MainThread", "rid": "-", "tid": reader_tid,
                 "since": time.perf_counter(), "depth": 1}]
    assert _reader_entries(polluted, reader_tid), "注入的读方条目必须被识别为串扰"
    with pytest.raises(AssertionError):
        assert not _reader_entries(polluted, reader_tid), "读方不应有自身条目"