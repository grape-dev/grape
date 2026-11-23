"""Issue #3927：全局 db_lock 长持有治理的回归用例（含审查修订回归）。

覆盖验收标准的关键切片：
- 任务级锁跨任务并行、同任务串行、用后回收（t_<id>）；
- 终态保护锁按事件维度统一（t_ev:<seq>）：传/不传 task_id 的终态写者互斥（审查 R1）；
- complete 终态写 + 大字段写都在事件锁内、MySQL 拓扑全程不持全局锁；
- payload 写失败登记 pending，重复 complete 短路时自愈补写（审查 R2）；
- SQLite/单库拓扑保留进程级短临界区（审查 R4）；
- update_task_status 大字段拆出 db_lock：3 并发 complete + 1 claim 不线性排队
  （spec 验收第 1 条，审查 R5 口径）；
- health refresh 的聚合计算在全局锁外执行（快照读 + 锁外聚合）；
- 慢锁观测：持锁栈 + waiter/holder 栈区分 + owner 维度指标有界（审查 R3）。

修复前（缺 lock_instrument / 事件锁 / 拆写 seam / pending 自愈 / 短临界区 guard）
这些用例应为红。
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
        """大字段写阻塞时，全局 db_lock 仍可立即获取（MySQL 拓扑口径，锁内只剩终态 UPDATE+payload 短写）。"""
        import src.domains.runtime.event_store as es

        monkeypatch.setattr(es, "_mysql_backend", lambda: True, raising=False)
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


class TestMixedLockScope:
    """审查 R1：混合锁口径——传/不传 task_id 的终态写者必须共用一把事件锁。"""

    def test_mixed_terminal_writers_serialize_on_same_event(self, es_db, monkeypatch):
        import src.domains.runtime.event_store as es

        monkeypatch.setattr(es, "_mysql_backend", lambda: True, raising=False)
        in_lock = threading.Event()
        proceed = threading.Event()
        state = {"paused": False}
        orig_cols = es._events_columns

        def _slow_cols(c):
            # 只阻塞首个进入者（首写者 A）：在终态保护 SELECT 之后、终态 UPDATE
            # 之前持锁暂停。后到写者不再走此 seam，其行为直接反映锁口径。
            if not state["paused"]:
                state["paused"] = True
                in_lock.set()
                proceed.wait(2.0)
            return orig_cols(c)

        monkeypatch.setattr(es, "_events_columns", _slow_cols)
        monkeypatch.setattr(es, "_notify_pipeline_failure", lambda *a, **kw: None)
        _insert_event(es_db, 3927004)

        t_a = threading.Thread(
            target=lambda: es.update_event_complete(
                3927004, "completed", exit_code=0,
                completed_at="2026-11-23T00:00:00+00:00", result="writer-A",
                agent_logs=[{"w": "A"}], task_id="3927-mix-a"),
            daemon=True)
        t_a.start()
        assert in_lock.wait(1.0), "写者 A 未进入终态保护临界区"

        # 写者 B：不传 task_id（判死/回收/接管同款调用面）
        t_b = threading.Thread(
            target=lambda: es.update_event_complete(
                3927004, "failed", exit_code=1,
                completed_at="2026-11-23T00:00:01+00:00", result="writer-B"),
            daemon=True)
        t_b.start()
        time.sleep(0.3)
        row = es_db.execute("SELECT status FROM events WHERE seq=3927004").fetchone()
        assert row[0] == "running", \
            "不传 task_id 的终态写者与带 task_id 写者未互斥（终态保护锁口径分裂）"

        proceed.set()
        t_a.join(2.0)
        t_b.join(2.0)
        row = es_db.execute(
            "SELECT status, result FROM events WHERE seq=3927004").fetchone()
        assert row[0] == "completed", "首写终态被后到写者覆盖"
        assert row[1] == "writer-A", "首写终态 result 被后到写者覆盖"


class TestSqliteTopologyGuard:
    """审查 R4：SQLite/单库共享连接下，终态+payload 写必须保留进程级短临界区。"""

    def test_sqlite_payload_write_holds_short_critical_section(self, es_db, monkeypatch):
        import src.domains.runtime.event_store as es

        monkeypatch.setattr(es, "_mysql_backend", lambda: False, raising=False)
        entered = threading.Event()
        proceed = threading.Event()
        orig = es._write_event_payload_fields

        def _slow_payload(seq, agent_logs, structured_output):
            entered.set()
            proceed.wait(2.0)
            return orig(seq, agent_logs, structured_output)

        monkeypatch.setattr(es, "_write_event_payload_fields", _slow_payload)
        _insert_event(es_db, 3927005)

        t = threading.Thread(
            target=lambda: es.update_event_complete(
                3927005, "completed", exit_code=0,
                completed_at="2026-11-23T00:00:00+00:00", result="done",
                agent_logs=[{"b": "x"}], task_id="3927-sqlite"),
            daemon=True)
        t.start()
        assert entered.wait(1.0), "payload 写 seam 未被调用"

        # SQLite/单库：commit 是连接级语义，终态+payload 短写必须在 db_lock 临界区内，
        # 否则与其他线程未提交语句发生跨线程事务交错
        assert not es.db_lock.acquire(timeout=0.4), \
            "SQLite 拓扑 payload 写脱离了进程级短临界区（跨线程事务交错风险）"

        proceed.set()
        t.join(2.0)
        row = es_db.execute("SELECT status FROM events WHERE seq=3927005").fetchone()
        assert row[0] == "completed"


class TestPayloadSelfHeal:
    """审查 R2：payload 写失败登记 pending，重复 complete 短路时自愈补写。"""

    def test_payload_failure_backfilled_on_duplicate_complete(self, es_db, monkeypatch):
        import src.domains.runtime.event_store as es

        orig = es._write_event_payload_fields
        state = {"fail_first": True}

        def _maybe_fail(seq, agent_logs, structured_output):
            if state["fail_first"]:
                state["fail_first"] = False
                return False  # 首写 payload 失败
            return orig(seq, agent_logs, structured_output)

        monkeypatch.setattr(es, "_write_event_payload_fields", _maybe_fail)
        monkeypatch.setattr(es, "_notify_pipeline_failure", lambda *a, **kw: None)
        _insert_event(es_db, 3927006)

        es.update_event_complete(
            3927006, "completed", exit_code=0,
            completed_at="2026-11-23T00:00:00+00:00", result="first",
            agent_logs=[{"line": "first"}],
            structured_output={"actions": ["first"], "conclusion": "first"},
            task_id="3927-heal")
        row = es_db.execute(
            "SELECT status, structured_output FROM events WHERE seq=3927006").fetchone()
        assert row[0] == "completed", "payload 失败不应回滚终态"
        assert row[1] is None, "首写 payload 失败却落库了"
        assert es.pending_payload_count() == 1, "payload 失败未登记 pending 标记"

        # 重复 complete（worker 重试 / 双主重复回写）→ 终态短路但补写大字段
        es.update_event_complete(
            3927006, "completed", exit_code=0,
            completed_at="2026-11-23T00:00:01+00:00", result="retry",
            agent_logs=[{"line": "retry"}],
            structured_output={"actions": ["retry"], "conclusion": "retry"},
            task_id="3927-heal")
        row = es_db.execute(
            "SELECT status, result, structured_output, agent_logs FROM events WHERE seq=3927006"
        ).fetchone()
        assert row[0] == "completed", "补写路径不应改终态"
        assert row[1] == "first", "补写路径不应覆盖首写终态 result"
        assert json.loads(row[2])["conclusion"] == "retry", "pending 自愈未补写 structured_output"
        assert json.loads(row[3]) == [{"line": "retry"}]
        assert es.pending_payload_count() == 0, "自愈补写后 pending 标记未清除"


class TestClaimNotBlockedByComplete:
    """spec 验收第 1 条（审查 R5）：并发 3 complete + 1 claim——claim 的 db_lock
    短临界区不随 complete 的大字段写线性排队。"""

    def test_claims_do_not_queue_behind_complete_big_writes(self, monkeypatch):
        from src.domains.runtime.engine import service as svc
        from src.kernel.session_manager import db_lock as session_db_lock

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute(
            "CREATE TABLE agent_tasks ("
            "id TEXT PRIMARY KEY, status TEXT, runtime_id INTEGER DEFAULT 0, "
            "event_seq INTEGER DEFAULT 0, completed_at TEXT, exit_code INTEGER, "
            "result_text TEXT, usage_json TEXT, errors TEXT, stderr TEXT)")
        task_ids = ["3927-claim-1", "3927-claim-2", "3927-claim-3"]
        for tid in task_ids:
            conn.execute(
                "INSERT INTO agent_tasks (id, status, runtime_id, event_seq) "
                "VALUES (?, 'running', 0, 0)", (tid,))
        conn.commit()
        monkeypatch.setattr(svc, "_rt_db", conn)

        entered = []
        proceed = threading.Event()

        def _slow_big_fields(task_id, cols):
            entered.append(task_id)
            proceed.wait(2.0)
            return True

        monkeypatch.setattr(svc, "_write_task_big_fields", _slow_big_fields)

        threads = [
            threading.Thread(
                target=lambda tid=tid: svc.update_task_status(
                    tid, "completed", exit_code=0,
                    result_text="x" * (1024 * 64), usage_json="{}"),
                daemon=True)
            for tid in task_ids
        ]
        for t in threads:
            t.start()
        assert _wait(lambda: len(entered) == 3, timeout=2.0), \
            "三个 complete 的大字段写未并行进入锁外短写段"

        # claim 模拟：claim 的临界区就是 session db_lock；大字段写期间必须可立即获取
        start = time.perf_counter()
        assert session_db_lock.acquire(timeout=1.0), \
            "complete 大字段写期间 db_lock 被持有——claim 排队随 complete 线性增长"
        session_db_lock.release()
        assert time.perf_counter() - start < 0.5, "claim 等锁时间随 complete 数量线性增长"

        proceed.set()
        for t in threads:
            t.join(2.0)
        row = conn.execute(
            "SELECT status, result_text FROM agent_tasks WHERE id=?", (task_ids[0],)).fetchone()
        assert row[0] == "completed", "终态短写未落库"
        # 大字段被 monkeypatch 短路，此处只验证终态先行
        conn.close()


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

    def test_slow_wait_logs_waiter_and_holder_stacks(self, caplog):
        """慢等待日志必须区分 waiter_stack（等待方）与 holder_stack(previous)（上一持锁方）。"""
        from src.kernel.lock_instrument import InstrumentedLock, reset_lock_metrics

        reset_lock_metrics()
        lock = InstrumentedLock("3927-wait-lock", slow_hold=999, slow_wait=0.05)

        def _hold():
            with lock:
                time.sleep(0.3)

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        time.sleep(0.1)
        with caplog.at_level("WARNING"):
            with lock:
                pass
        holder.join(2.0)

        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "waiter_stack" in messages, "慢等待未输出等待方栈"
        assert "holder_stack(previous)" in messages, "慢等待未输出上一持锁方栈"
        assert "_hold" in messages, "holder_stack(previous) 不是上一持锁方的栈（指错方向）"
        assert any("slow wait" in r.getMessage() and "wait=0." in r.getMessage()
                   for r in caplog.records), "慢等待日志缺 wait 耗时字段"


class TestBoundedMetrics:
    """审查 R3：指标 (lock, owner) 键基数有界——粗粒度锁名 + owner 折叠 + 容量上限。"""

    def test_task_lock_metrics_coarse_grained(self):
        from src.kernel import lock_instrument as li

        li.reset_lock_metrics()
        for tid in ("3927-metrics-a", "3927-metrics-b", "3927-metrics-c"):
            with li.task_lock(tid):
                pass
        snap = li.lock_metrics_snapshot()
        assert snap, "任务锁使用后无指标条目"
        assert all(e["lock"] == "task_lock" for e in snap), \
            "任务锁指标锁名未粗化（仍按 task:<id> 一任务一条）"
        assert all(e["owner"] == "task:*" for e in snap), \
            "任务锁 owner 尾段实体 id 未折叠为 task:*（指标基数随任务数爆炸）"

    def test_event_lock_metrics_coarse_grained(self):
        from src.kernel import lock_instrument as li

        li.reset_lock_metrics()
        for seq in (39270001, 39270002):
            with li.event_lock(seq):
                pass
        snap = li.lock_metrics_snapshot()
        assert all(e["lock"] == "event_lock" for e in snap), \
            "事件锁指标锁名未粗化（仍按 event:<seq> 一事件一条）"
        assert all(e["owner"] == "event:*" for e in snap), \
            "事件锁 owner 尾段 seq 未折叠（指标基数随事件数爆炸）"

    def test_metrics_entries_capped_with_overflow(self, monkeypatch):
        from src.kernel import lock_instrument as li

        li.reset_lock_metrics()
        monkeypatch.setenv("LOCK_METRICS_MAX_ENTRIES", "16")
        lock = li.InstrumentedLock("3927-cap-lock", slow_hold=999, slow_wait=999)
        for i in range(40):
            with li.lock_owner(f"owner-{i}"):
                lock.acquire()
                lock.release()
        snap = [e for e in li.lock_metrics_snapshot() if e["lock"] == "3927-cap-lock"]
        assert len(snap) <= 17, f"指标条目数未受容量上限约束（{len(snap)} 条）"
        assert any(e["owner"] == "__overflow__" for e in snap), \
            "超限新键未折叠进 __overflow__ 桶（长稳进程指标字典无界增长）"