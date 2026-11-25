"""pending_self_report 的三处收敛缺口（事件侧活跃判据 / 执行痕迹判据 / 自报宽限）。

复现链路（2026-11-24 实测，repo 内 issue #5 的流水线）：

1. 主控重启时 `terminate_active_agents` 把仍在执行的 REMOTE 任务批量置
   `pending_self_report`（等 worker 自报），任务从此不属于 queued/dispatched/running；
2. 事件层 sweeper 只把这三个状态当「活跃」，于是「任务还在写执行消息、事件已超 30 分钟」
   的事件被判 failed（实测 coding 事件 18:12 判失败，同一任务 18:15 仍在写消息）；
3. 任务层 sweeper 用 `agent_tasks.runtime_session_id` 判「有没有建立过引擎会话」，但该列
   在 ACP/pipeline 链路上不写（全库 21375 条只有 4575 条非空、issue 类 3325 条只有 228 条
   非空）→ 有 42 条消息、结论齐全的任务照样走 600s 短窗判死；
4. self_report 已落库（`self_reported=1`）但没有 `stream_end` 时，没有任何路径写终态，
   只能等判死——agent 的工作被记成 timeout_failed。

本文件覆盖：
- 事件侧：pending_self_report + 活跃执行流 → 不判 failed；静默 → 照旧回收；
- 任务侧：判据换成执行痕迹（消息/会话指针），真有痕迹走 5400s 窗口，无痕迹才走 600s；
- 任务侧：self_report 已在、stream_end 丢失 → 宽限期满收敛 completed（保留 result_text）；
- 回归：既有超时判死、终态文案、活跃流豁免不退化。
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import cleanup

RT = 9331
CRED = 9431
RK = "gitcode:selftest/pending-convergence"
BASE_SEQ = 10900

# 与实现常量对齐
SELF_REPORT_TIMEOUT = 5400
NO_TRACE_TIMEOUT = 600
STREAM_ACTIVE_WINDOW = 600


@pytest.fixture(autouse=True)
def _past_restart_window(monkeypatch):
    """Issue #4303：sweep_stuck_events_once 有以进程启动时刻为参照的重启恢复
    窗口护栏；本文件测回收判据本身，把窗口钉到已过期。"""
    monkeypatch.setattr(cleanup, "_PROCESS_STARTED_AT", time.time() - 3600)


def _utcnow():
    return datetime.now(timezone.utc)


def _clean():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM task_messages WHERE task_id LIKE 't_psc%'")
        except Exception:
            pass
        for t in ("agent_tasks", "issue_agent_session"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (RK,))
            except Exception:
                pass
        try:
            c.execute("DELETE FROM events WHERE seq >= ? AND seq < ?", (BASE_SEQ, BASE_SEQ + 100))
        except Exception:
            pass
        try:
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (RT,))
        except Exception:
            pass
        db.commit()


def _seed_runtime():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (RT,))
        c.execute(
            "INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
            "busy_slots, max_concurrent, last_seen_at) VALUES (?,?,?,?,1,1,?,?,?)",
            (RT, f"rt-{RT}", CRED, "online", 1, 4, _utcnow().isoformat()))
        db.commit()


def _seed_event(seq, started_ago_seconds=3600, status="running"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status, "
            "title, summary, result, started_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (seq, RK, "#5", "a2a_event", "coding", status, "t", "A2A: → coding", "",
             (_utcnow() - timedelta(seconds=started_ago_seconds)).isoformat()))
        db.commit()


def _seed_task(tid, seq, status="pending_self_report", pending_ago_seconds=1900,
               runtime_session_id="", self_reported=0, result_text=""):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, "
            "agent_type, event_type, status, prompt, cwd, skill_files, env_extra, event_seq, "
            "skill, started_at, pending_since, runtime_session_id, self_reported, result_text) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "sess-" + tid, RT, RK, "#5", "coding", "a2a_event", status,
             "pipeline task", "", "{}", "{}", seq, "coding-cli",
             (_utcnow() - timedelta(seconds=pending_ago_seconds + 600)).isoformat(),
             (_utcnow() - timedelta(seconds=pending_ago_seconds)).isoformat(),
             runtime_session_id, self_reported, result_text))
        db.commit()


def _seed_message(tid, age_seconds):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO task_messages (task_id, seq, type, tool, content, input_json, output, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (tid, 1, "text", "", "执行中", "", "",
             (_utcnow() - timedelta(seconds=age_seconds)).isoformat()))
        db.commit()


def _task(tid):
    with db_lock:
        row = db.cursor().execute(
            "SELECT status, exit_code, errors, result_text FROM agent_tasks WHERE id=?",
            (tid,)).fetchone()
    return dict(zip(("status", "exit_code", "errors", "result_text"), row))


def _event_status(seq):
    with db_lock:
        row = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
    return row[0] if row else None


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    _seed_runtime()
    yield
    _clean()


# =====================================================================
# 第 1 处：事件 sweeper 认 pending_self_report（按执行流活跃度）
# =====================================================================

class TestEventSweeperPendingSelfReport:

    def test_active_pending_task_keeps_event_running(self):
        """任务 pending_self_report 但 60s 前还在写消息 → 事件不得被判 failed。"""
        from src.domains.runtime.engine.cleanup import sweep_stuck_events_once
        _seed_event(BASE_SEQ + 1)
        _seed_task("t_psc_ev1", BASE_SEQ + 1)
        _seed_message("t_psc_ev1", age_seconds=60)
        sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        assert _event_status(BASE_SEQ + 1) == "running", \
            "worker 仍在产执行流（600s 活跃窗内）时，事件不得先被判 failed"

    def test_silent_pending_task_still_reaped(self):
        """任务 pending_self_report 且执行流已静默 700s → 事件照旧回收（兜底不削弱）。"""
        from src.domains.runtime.engine.cleanup import sweep_stuck_events_once
        _seed_event(BASE_SEQ + 2)
        _seed_task("t_psc_ev2", BASE_SEQ + 2)
        _seed_message("t_psc_ev2", age_seconds=700)
        sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        assert _event_status(BASE_SEQ + 2) == "failed"

    def test_pending_task_without_messages_reaped(self):
        """数据缺失不豁免：pending 任务一条消息都没有 → 事件照旧回收。"""
        from src.domains.runtime.engine.cleanup import sweep_stuck_events_once
        _seed_event(BASE_SEQ + 3)
        _seed_task("t_psc_ev3", BASE_SEQ + 3)
        sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        assert _event_status(BASE_SEQ + 3) == "failed"


# =====================================================================
# 第 2 处：判活判据换成「执行痕迹」，不再只看 runtime_session_id
# =====================================================================

class TestExecutionTraceCriterion:

    def test_messages_count_as_trace_so_short_fuse_not_applied(self):
        """有 42 条消息但 runtime_session_id 为空、静默 700s、pending 1900s → 不判死。"""
        from src.domains.runtime.engine.scheduler import _sweep_pending_self_report_timeouts
        _seed_event(BASE_SEQ + 4)
        _seed_task("t_psc_tr1", BASE_SEQ + 4, pending_ago_seconds=1900,
                   runtime_session_id="")
        _seed_message("t_psc_tr1", age_seconds=700)
        _sweep_pending_self_report_timeouts()
        assert _task("t_psc_tr1")["status"] == "pending_self_report", \
            "有执行消息即算建立过会话，应走 5400s 窗口而不是 600s 短窗"

    def test_no_trace_at_all_still_uses_short_fuse(self):
        """无会话指针、无消息、pending 700s → 仍按 600s 短窗判死，文案不再谎称无消息。"""
        from src.domains.runtime.engine.scheduler import _sweep_pending_self_report_timeouts
        _seed_event(BASE_SEQ + 5)
        _seed_task("t_psc_tr2", BASE_SEQ + 5, pending_ago_seconds=700)
        assert _sweep_pending_self_report_timeouts() >= 1
        t = _task("t_psc_tr2")
        assert t["status"] == "timeout_failed"
        assert "PENDING_NO_SESSION_TIMEOUT" in t["errors"]
        assert "task_messages 无记录" in t["errors"], "文案应写明真实判据"
        assert "无任务消息" not in t["errors"]

    def test_issue_session_pointer_counts_as_trace(self):
        """agent_tasks 该列为空，但 issue 会话指针有值 → 也算执行痕迹。"""
        from src.domains.runtime.engine.scheduler import _sweep_pending_self_report_timeouts
        _seed_event(BASE_SEQ + 6)
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue_agent_session (id, repo_key, agent_type, issue_id, "
                "runtime_session_id, session_engine, status) VALUES (?,?,?,?,?,?,?)",
                ("isess-psc-1", RK, "coding", "#5", "ses-abc123", "opencode", "active"))
            db.commit()
        _seed_task("t_psc_tr3", BASE_SEQ + 6, pending_ago_seconds=1900)
        with db_lock:
            db.cursor().execute(
                "UPDATE agent_tasks SET issue_session_id=? WHERE id=?",
                ("isess-psc-1", "t_psc_tr3"))
            db.commit()
        _sweep_pending_self_report_timeouts()
        assert _task("t_psc_tr3")["status"] == "pending_self_report"


# =====================================================================
# 第 3 处：self_report 已在、stream_end 丢失 → 宽限后按自报结论收敛
# =====================================================================

class TestSelfReportOnlyConvergence:

    def test_self_reported_converges_completed_after_stream_silence(self):
        """self_reported=1、执行流静默超活跃窗、stream_end 未到 → completed 且保留 result_text。

        与审查 C2 对应：判据是「执行流静默超过活跃窗」这个真实信号，不看 pending_since——
        pending 已 3600s 也不影响结论。
        """
        from src.domains.runtime.engine.scheduler import _sweep_pending_self_report_timeouts
        _seed_event(BASE_SEQ + 7)
        payload = '{"actions": ["x"], "conclusion": "ready", "next_step": "a2a_coding"}'
        _seed_task("t_psc_sr1", BASE_SEQ + 7, pending_ago_seconds=3600,
                   self_reported=1, result_text=payload)
        _seed_message("t_psc_sr1", age_seconds=700)
        assert _sweep_pending_self_report_timeouts() >= 1
        t = _task("t_psc_sr1")
        assert t["status"] == "completed"
        assert t["exit_code"] == 0
        assert t["result_text"] == payload, "agent 自报的结构化结论必须原样保留"
        assert "SELF_REPORTED_STREAM_LOST" in t["errors"]
        assert _event_status(BASE_SEQ + 7) == "completed", "任务收敛后事件应级联 completed"

    def test_self_reported_but_young_pending_still_waits_window(self):
        """自报已在、但执行流静默还没满活跃窗 → 不动（等 stream_end 正常收尾）。"""
        from src.domains.runtime.engine.scheduler import _sweep_pending_self_report_timeouts
        _seed_event(BASE_SEQ + 8)
        _seed_task("t_psc_sr2", BASE_SEQ + 8, pending_ago_seconds=3600,
                   self_reported=1, result_text='{"conclusion": "ready"}')
        _seed_message("t_psc_sr2", age_seconds=300)
        _sweep_pending_self_report_timeouts()
        assert _task("t_psc_sr2")["status"] == "pending_self_report", \
            "静默未满活跃窗就不得定案（C2：自报后仍在执行的任务不能被提前判定完成）"

    def test_self_reported_with_active_stream_not_converged(self):
        """自报已在但执行流仍在写（<600s）→ 不收敛，继续豁免。"""
        from src.domains.runtime.engine.scheduler import _sweep_pending_self_report_timeouts
        _seed_event(BASE_SEQ + 9)
        _seed_task("t_psc_sr3", BASE_SEQ + 9, pending_ago_seconds=600,
                   self_reported=1, result_text='{"conclusion": "ready"}')
        _seed_message("t_psc_sr3", age_seconds=30)
        _sweep_pending_self_report_timeouts()
        assert _task("t_psc_sr3")["status"] == "pending_self_report"


# =====================================================================
# 回归：既有语义不退化
# =====================================================================

class TestRegressions:

    def test_timeout_still_reaps_silent_task_past_long_window(self):
        """静默超 5400s 且未自报 → 仍然 timeout_failed（兜底保留）。"""
        from src.domains.runtime.engine.scheduler import _sweep_pending_self_report_timeouts
        _seed_event(BASE_SEQ + 10)
        _seed_task("t_psc_rg1", BASE_SEQ + 10, pending_ago_seconds=5500,
                   runtime_session_id="ses-old")
        _seed_message("t_psc_rg1", age_seconds=5600)
        assert _sweep_pending_self_report_timeouts() >= 1
        assert _task("t_psc_rg1")["status"] == "timeout_failed"

    def test_queued_task_keeps_event_live(self):
        """queued/dispatched/running 仍是活跃任务（原判据不丢）。"""
        from src.domains.runtime.engine.cleanup import sweep_stuck_events_once
        _seed_event(BASE_SEQ + 11)
        _seed_task("t_psc_rg2", BASE_SEQ + 11, status="running", pending_ago_seconds=0)
        sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        assert _event_status(BASE_SEQ + 11) == "running"

    def test_active_window_single_source(self):
        """活跃窗只有一个来源：任务侧与事件侧读到同一个值（审查 M1）。"""
        import src.domains.runtime.engine.scheduler as sched
        from src.domains.runtime.engine import cleanup as cl, liveness
        assert liveness.PENDING_STREAM_ACTIVE_WINDOW_SECONDS == STREAM_ACTIVE_WINDOW
        assert cl._PENDING_STREAM_ACTIVE_WINDOW_SECONDS is liveness.PENDING_STREAM_ACTIVE_WINDOW_SECONDS
        assert sched._PENDING_STREAM_ACTIVE_WINDOW_SECONDS is liveness.PENDING_STREAM_ACTIVE_WINDOW_SECONDS

    def test_liveness_env_invalid_falls_back(self, monkeypatch):
        """env 非法整数不得打挂 import（审查 I3）。"""
        import importlib
        import src.domains.runtime.engine.liveness as lv
        monkeypatch.setenv("PENDING_STREAM_ACTIVE_WINDOW_SECONDS", "abc")
        try:
            importlib.reload(lv)
            assert lv.PENDING_STREAM_ACTIVE_WINDOW_SECONDS == STREAM_ACTIVE_WINDOW
        finally:
            monkeypatch.delenv("PENDING_STREAM_ACTIVE_WINDOW_SECONDS", raising=False)
            importlib.reload(lv)


class TestEventProbeSharding:
    """审查 M2：探针按批查询，大批量不撞参数上限且不整批放弃。"""

    def test_large_batch_still_detects_active_event(self):
        from src.domains.runtime.engine.cleanup import _pending_self_report_live_seqs
        from src.kernel.session_manager import db, db_lock
        # 造 5 个事件/任务，活跃那个排最后（分片后会落在第二批）
        for i in range(5):
            seq = BASE_SEQ + 20 + i
            _seed_event(seq)
            _seed_task(f"t_psc_sh{i}", seq, pending_ago_seconds=600)
            _seed_message(f"t_psc_sh{i}", age_seconds=60 if i == 4 else 900)
        with db_lock:
            c = db.cursor()
            live = _pending_self_report_live_seqs(c, [BASE_SEQ + 20 + i for i in range(5)], batch=2)
        assert live == {BASE_SEQ + 24}, f"分片后仍应只命中活跃那条，实际 {live}"

    def test_probe_exception_is_isolated_and_logged(self, caplog):
        from src.domains.runtime.engine.cleanup import _pending_self_report_live_seqs
        import logging

        class _Boom:
            def execute(self, *a, **k):
                raise RuntimeError("bind limit")

            def fetchall(self):
                return []

        _seed_event(BASE_SEQ + 30)
        _seed_task("t_psc_boom", BASE_SEQ + 30, pending_ago_seconds=600)
        with caplog.at_level(logging.ERROR, logger="devops-bot"):
            assert _pending_self_report_live_seqs(_Boom(), [BASE_SEQ + 30]) == set()
        assert any("pending task probe failed" in r.message for r in caplog.records), \
            "探测失败要留 error 级日志（可观测）"