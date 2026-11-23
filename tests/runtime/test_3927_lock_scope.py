"""Issue #3927：全局 db_lock 长持有治理的回归用例。

覆盖验收标准的关键切片：
- 任务级锁跨任务并行、同任务串行、用后回收（t_<id>）；
- complete 终态写走任务级锁、大字段拆写（写大字段时不再持全局锁）；
- 同任务重复 complete 幂等短路，不覆盖首次终态/大字段；
- health refresh 的聚合计算在全局锁外执行（快照读 + 锁外聚合）；
- 慢锁观测（超阈值打印持锁栈 + owner 维度指标）。

修复前（无 task_lock / 无拆写 / 无锁外聚合）这些用例应为红。
"""
import json
import os
import sqlite3
import tempfile
import threading
import time

import pytest


def _wait(pred, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# ---------------------------------------------------------------- task lock

class TestTaskLock:
    def test_distinct_tasks_do_not_block(self):
        from src.kernel.lock_instrument import task_lock

        held = threading.Event()
        release = threading.Event()

        def _hold_a():
            with task_lock("3927-a"):
                held.set()
                release.wait(2.0)

        t = threading.Thread(target=_hold_a, daemon=True)
        t.start()
        assert held.wait(1.0), "holder 未进入 task_lock('3927-a')"

        entered = threading.Event()

        def _enter_b():
            with task_lock("3927-b"):
                entered.set()

        t2 = threading.Thread(target=_enter_b, daemon=True)
        t2.start()
        assert entered.wait(1.0), "不同任务的 complete 被全局串行（应跨任务并行）"
        release.set()
        t.join(2.0)
        t2.join(2.0)

    def test_same_task_serializes(self):
        from src.kernel.lock_instrument import task_lock

        order = []
        inside = threading.Event()
        release = threading.Event()

        def _first():
            with task_lock("3927-same"):
                inside.set()
                order.append("first-in")
                release.wait(2.0)
                order.append("first-out")

        t = threading.Thread(target=_first, daemon=True)
        t.start()
        assert inside.wait(1.0)

        def _second():
            with task_lock("3927-same"):
                order.append("second-in")

        t2 = threading.Thread(target=_second, daemon=True)
        t2.start()
        time.sleep(0.2)
        assert "second-in" not in order, "同任务未串行（第二个 writer 提前进入）"
        release.set()
        t.join(2.0)
        t2.join(2.0)
        assert order == ["first-in", "first-out", "second-in"]

    def test_registry_reclaims_after_release(self):
        from src.kernel.lock_instrument import task_lock, task_lock_count

        before = task_lock_count()
        with task_lock("3927-reclaim"):
            assert task_lock_count() == before + 1
        assert task_lock_count() == before, "任务级锁用后未回收（引用计数泄漏）"


# ------------------------------------------------------------- event store

@pytest.fixture
def es_db(monkeypatch):
    """临时事件库：隔离全局连接、屏蔽终态钩子的副作用。"""
    import src.domains.runtime.event_store as es

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name, check_same_thread=False)
    conn.execute(
        "CREATE TABLE events ("
        "seq INTEGER PRIMARY KEY, repo_key TEXT, issue_id TEXT, event_type TEXT, "
        "source TEXT, agent_type TEXT, title TEXT, actor TEXT, summary TEXT, "
        "payload TEXT, status TEXT, result TEXT, runtime TEXT, skill TEXT, "
        "exit_code INTEGER, duration REAL, agent_logs TEXT, structured_output TEXT, "
        "failed_node TEXT, started_at TEXT, completed_at TEXT, created_at TEXT)"
    )
    conn.commit()

    monkeypatch.setattr(es, "db", conn)
    monkeypatch.setattr(es, "_release_guard_by_seq", lambda seq: None)
    monkeypatch.setattr(es, "_drain_a2a_queue_after_terminal", lambda seq: None)
    monkeypatch.setattr(es, "_notify_pipeline_failure", lambda *a, **kw: None)
    old_cache = es._EVENTS_COLUMNS_CACHE
    es._EVENTS_COLUMNS_CACHE = None
    try:
        yield conn
    finally:
        es._EVENTS_COLUMNS_CACHE = old_cache
        conn.close()
        os.unlink(tmp.name)


def _insert_event(conn, seq, status="running"):
    conn.execute(
        "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status) "
        "VALUES (?, 'gitcode:o/r', '#3927', 'a2a_event', 'coding', ?)",
        (seq, status),
    )
    conn.commit()


class TestCompleteSplitWrite:
    def test_complete_accepts_task_id_and_writes_big_fields(self, es_db):
        from src.domains.runtime.event_store import update_event_complete

        _insert_event(es_db, 3927001)
        update_event_complete(
            3927001, "completed", exit_code=0, duration=1.5,
            completed_at="2026-11-23T00:00:00+00:00", result="done",
            agent_logs=[{"line": "x"}],
            structured_output={"actions": ["a"], "conclusion": "c"},
            task_id="3927-t1",
        )
        row = es_db.execute(
            "SELECT status, agent_logs, structured_output FROM events WHERE seq=3927001"
        ).fetchone()
        assert row[0] == "completed"
        assert json.loads(row[1]) == [{"line": "x"}]
        assert json.loads(row[2])["conclusion"] == "c"

    def test_duplicate_complete_is_idempotent_and_keeps_first_payload(self, es_db):
        from src.domains.runtime.event_store import update_event_complete

        _insert_event(es_db, 3927002)
        update_event_complete(
            3927002, "completed", exit_code=0,
            completed_at="2026-11-23T00:00:00+00:00", result="first",
            agent_logs=[{"line": "first"}],
            structured_output={"actions": ["first"], "conclusion": "first"},
            task_id="3927-t2",
        )
        start = time.perf_counter()
        update_event_complete(
            3927002, "failed", exit_code=1,
            completed_at="2026-11-23T00:00:01+00:00", result="second",
            agent_logs=[{"line": "second"}],
            structured_output={"actions": ["second"], "conclusion": "second"},
            task_id="3927-t2",
        )
        elapsed = time.perf_counter() - start
        row = es_db.execute(
            "SELECT status, result, agent_logs FROM events WHERE seq=3927002"
        ).fetchone()
        assert row[0] == "completed", "重复 complete 覆盖了已终态"
        assert row[1] == "first"
        assert json.loads(row[2]) == [{"line": "first"}], "幂等短路后仍改写了首写大字段"
        assert elapsed < 1.0, f"重复 complete 未快速返回（{elapsed:.3f}s）"

    def test_big_field_write_does_not_hold_global_lock(self, es_db, monkeypatch):
        """大字段写阻塞时，全局 db_lock 仍可立即获取（锁内只剩终态 UPDATE）。"""
        import src.domains.runtime.event_store as es

        entered = threading.Event()
        proceed = threading.Event()
        orig = es._write_event_payload_fields

        def _slow_payload(seq, agent_logs, structured_output):
            entered.set()
            proceed.wait(2.0)
            return orig(seq, agent_logs, structured_output)

        monkeypatch.setattr(es, "_write_event_payload_fields", _slow_payload)
        _insert_event(es_db, 3927003)

        t = threading.Thread(
            target=lambda: es.update_event_complete(
                3927003, "completed", exit_code=0,
                completed_at="2026-11-23T00:00:00+00:00", result="done",
                agent_logs=[{"big": "x" * 1024}],
                structured_output={"actions": ["a"], "conclusion": "c"},
                task_id="3927-t3",
            ),
            daemon=True,
        )
        t.start()
        assert entered.wait(1.0), "大字段写 seam 未被调用"

        start = time.perf_counter()
        assert es.db_lock.acquire(timeout=1.0), "大字段写期间全局锁被持有（claim 会被堵）"
        es.db_lock.release()
        assert time.perf_counter() - start < 0.5

        proceed.set()
        t.join(2.0)


# -------------------------------------------------------------- health calc

class TestHealthAggregateOffLock:
    def test_aggregation_runs_outside_global_lock(self, monkeypatch):
        from src.domains.runtime.engine import health_scorer as hs

        started = threading.Event()
        proceed = threading.Event()
        orig = hs._aggregate_task_rows

        def _slow_agg(rows, decay, last_status=""):
            started.set()
            proceed.wait(2.0)
            return orig(rows, decay, last_status=last_status)

        monkeypatch.setattr(hs, "_aggregate_task_rows", _slow_agg)
        monkeypatch.setattr(hs, "_snapshot_task_rows",
                            lambda rid, window: ([("completed", "2026-11-23T00:00:00")], "completed"))
        monkeypatch.setattr(hs, "_effective_window_start", lambda rid: "2026-11-23T00:00:00")

        t = threading.Thread(target=lambda: hs._fetch_task_stats(1), daemon=True)
        t.start()
        assert started.wait(1.0), "聚合 seam 未被调用"

        start = time.perf_counter()
        lock = hs._lock()
        assert lock.acquire(timeout=1.0), "聚合计算期间仍持全局锁（refresh 会堵 complete）"
        lock.release()
        assert time.perf_counter() - start < 0.5

        proceed.set()
        t.join(2.0)


# ------------------------------------------------------ auto-advance async

class TestAutoAdvanceAsync:
    def test_flag_default_off_and_opt_in(self, monkeypatch):
        from src.domains.runtime.engine import service as svc

        monkeypatch.delenv("COMPLETE_A2A_ASYNC", raising=False)
        assert svc._auto_advance_async_enabled() is False
        monkeypatch.setenv("COMPLETE_A2A_ASYNC", "1")
        assert svc._auto_advance_async_enabled() is True

    def test_schedule_runs_in_background_when_enabled(self, monkeypatch):
        from src.domains.runtime.engine import service as svc

        monkeypatch.setenv("COMPLETE_A2A_ASYNC", "1")
        done = threading.Event()
        monkeypatch.setattr(svc, "_run_auto_advance_after_complete",
                            lambda *a, **kw: done.set())
        out = svc._schedule_auto_advance_after_complete({"id": "3927-async"}, None, "")
        assert out is None, "异步模式不应在请求线程内返回推进结果"
        assert done.wait(1.0), "后台 A2A 推进线程未启动"


# -------------------------------------------------------- slow lock metrics

class TestSlowLockObservability:
    def test_slow_hold_logs_stack_and_metrics(self, caplog):
        from src.kernel.lock_instrument import (
            InstrumentedLock, lock_metrics_snapshot, lock_owner, reset_lock_metrics,
        )

        reset_lock_metrics()
        lock = InstrumentedLock("3927-test-lock", slow_hold=0.05, slow_wait=0.05)
        with caplog.at_level("WARNING"):
            with lock_owner("unit-owner"):
                with lock:
                    time.sleep(0.12)

        snap = lock_metrics_snapshot()
        entry = next((e for e in snap if e["lock"] == "3927-test-lock"
                      and e["owner"] == "unit-owner"), None)
        assert entry is not None, "慢锁指标未按 owner 维度记录"
        assert entry["slow_hold"] >= 1
        assert entry["max_hold"] >= 0.05
        assert any("holder_stack" in r.getMessage() for r in caplog.records), \
            "慢持有未打印持锁栈"

    def test_reentrant_lock_records_single_hold(self):
        from src.kernel.lock_instrument import (
            InstrumentedLock, lock_metrics_snapshot, lock_owner, reset_lock_metrics,
        )

        reset_lock_metrics()
        lock = InstrumentedLock("3927-reentrant", slow_hold=999, slow_wait=999)
        with lock_owner("reentrant-owner"):
            with lock:
                with lock:
                    pass
        entry = next(e for e in lock_metrics_snapshot()
                     if e["lock"] == "3927-reentrant" and e["owner"] == "reentrant-owner")
        assert entry["hold_count"] == 1, "可重入锁嵌套 acquire 被重复计为多次持有"