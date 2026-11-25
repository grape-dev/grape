"""Issue #3741：Autopilot 无进展轮询死循环回归。

覆盖点（对齐 Spec）：
- 连续 N 步 tool+input 指纹相同 → no_progress 巡检命中（不依赖「静默时长」，避免 #1495 误杀）；
- 重派上限耗尽 → failed(reason=no_progress) 收口，error 带步号/指纹，并释放 busy 槽；
- poll 超时 → 先终止仍活跃的 agent_task，再写 exec failed；
- 反向残留（exec 已终态 ∧ agent_task 仍活跃）→ periodic sweep 终止任务。
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import scheduler
from src.domains.runtime.engine import task_activity as ta
from src.domains.runtime.engine import task_timeout as tt

RK = "gitcode:issue3741/no-progress"
SEQ_BASE = 94101
RT = 37411
CID = 0


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("RUNTIME_NO_PROGRESS_TIMEOUT", "RUNTIME_NO_PROGRESS_RETRIES",
              "RUNTIME_NO_PROGRESS_ENABLED", "RUNTIME_NO_PROGRESS_REPEAT_STEPS",
              "GRAPE_TASK_TIMEOUT_CONFIG", "TERMINAL_EXEC_REVERSE_GRACE_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    _clean()
    yield
    _clean()


def _clean():
    with db_lock:
        c = db.cursor()
        for sql, args in (
            ("DELETE FROM task_messages WHERE task_id LIKE 't_i3741_%'", ()),
            ("DELETE FROM agent_tasks WHERE id LIKE 't_i3741_%'", ()),
            ("DELETE FROM autopilot_executions WHERE id >= ? AND id < ?",
             (SEQ_BASE, SEQ_BASE + 200)),
            ("DELETE FROM events WHERE seq >= ? AND seq < ?", (SEQ_BASE, SEQ_BASE + 200)),
            ("DELETE FROM agent_runtimes WHERE id=?", (RT,)),
        ):
            try:
                c.execute(sql, args)
            except Exception:
                pass
        db.commit()


def _iso_ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _seed_runtime(rid=RT, busy=1, status="online"):
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        for col, ddl in (("busy_slots", "INTEGER DEFAULT 0"),
                         ("max_concurrent", "INTEGER DEFAULT 4"),
                         ("worker_version", "TEXT DEFAULT ''")):
            if col not in cols:
                c.execute(f"ALTER TABLE agent_runtimes ADD COLUMN {col} {ddl}")
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        c.execute(
            "INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
            "busy_slots, max_concurrent, last_seen_at, worker_version) "
            "VALUES (?,?,?,?,1,1,?,?,?,'')",
            (rid, f"rt-{rid}", CID, status, busy, 4, _iso_ago(0)))
        db.commit()


def _seed_task(tid, seq, started_secs_ago=900, status="running", runtime_id=RT,
               errors=None, event_type="autopilot_event", skill="coding", env_extra="{}"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, cwd, skill_files, env_extra, event_seq, skill, "
            "started_at, errors) VALUES (?,?,?,?, 'coding', ?, ?, 'x', '', '{}', ?, ?, ?, ?, ?)",
            (tid, "sess-" + tid, runtime_id, RK, event_type, status, env_extra, seq, skill,
             _iso_ago(started_secs_ago), json.dumps(errors) if errors is not None else None))
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, agent_type, status, summary, result) "
            "VALUES (?,?,?,'coding','running','A2A: → coding','')",
            (seq, RK, "autopilot_event"))
        db.commit()


def _seed_messages(tid, specs):
    """specs: [(type, tool, input_json, secs_ago), ...] —— seq 按列表顺序从 1 递增。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM task_messages WHERE task_id=?", (tid,))
        for i, (typ, tool, inp, ago) in enumerate(specs, start=1):
            c.execute(
                "INSERT INTO task_messages (task_id, seq, type, tool, input_json, content, created_at) "
                "VALUES (?,?,?,?,?,'x',?)",
                (tid, i, typ, tool, inp, _iso_ago(ago)))
        db.commit()


def _repeated_spec(n=8, ago=5, tool="bash",
                   inp='{"command": "sleep 115; tail -5 /tmp/x.log; pgrep -f act.py"}'):
    return [("tool_use", tool, inp, ago) for _ in range(n)]


def _seed_exec(exec_id, atid, status="running", started_secs_ago=900,
               completed_secs_ago=None):
    _started = _iso_ago(started_secs_ago)
    _completed = _iso_ago(completed_secs_ago) if completed_secs_ago is not None else None
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (exec_id,))
        c.execute(
            "INSERT INTO autopilot_executions "
            "(id, task_id, status, a2a_seq, agent_task_id, started_at, completed_at, created_at) "
            "VALUES (?,77,?,?,?,?,?,?)",
            (exec_id, status, exec_id, str(atid), _started, _completed, _started))
        db.commit()


def _task_row(tid):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, started_at, errors, runtime_id FROM agent_tasks WHERE id=?",
            (tid,)).fetchone()


def _exec_row(exec_id):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, error FROM autopilot_executions WHERE id=?", (exec_id,)).fetchone()


def _busy(rid=RT):
    with db_lock:
        return db.cursor().execute(
            "SELECT busy_slots FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]


def _diag(raw, code):
    try:
        items = json.loads(raw or "[]")
    except Exception:
        return None
    for e in items:
        if isinstance(e, dict) and e.get("error_code") == code:
            return e
    return None


def _codes(raw):
    try:
        return [e.get("error_code") for e in json.loads(raw or "[]") if isinstance(e, dict)]
    except Exception:
        return []


# ── 配置链 ─────────────────────────────────────────────────────

class TestRepeatStepsConfig:
    def test_default_is_eight(self):
        assert tt.no_progress_repeat_steps({"skill": "s", "repo_key": "r"}, cfg={}) == 8

    def test_task_env_zero_disables(self):
        t = {"skill": "s", "repo_key": "r",
             "env_extra": json.dumps({"RUNTIME_NO_PROGRESS_REPEAT_STEPS": "0"})}
        assert tt.no_progress_repeat_steps(t, cfg={"no_progress_repeat_steps": 4}) == 0

    def test_top_level_and_bad_value(self):
        t = {"skill": "s", "repo_key": "r"}
        assert tt.no_progress_repeat_steps(t, cfg={"no_progress_repeat_steps": 5}) == 5
        assert tt.no_progress_repeat_steps(t, cfg={"no_progress_repeat_steps": -3}) == 8
        assert tt.no_progress_repeat_steps(t, cfg={"no_progress_repeat_steps": "bad"}) == 8


# ── 指纹判据 ───────────────────────────────────────────────────

class TestRepeatedToolSteps:
    def _call(self, tid, min_repeats=8, window=600):
        with db_lock:
            return ta.repeated_tool_steps(db.cursor(), tid, window, min_repeats,
                                          now_epoch=time.time())

    def test_identical_steps_hit(self):
        tid = "t_i3741_rep_hit"
        _seed_messages(tid, _repeated_spec(8))
        got = self._call(tid)
        assert got is not None
        assert got["repeats"] == 8
        assert got["first_seq"] == 1 and got["last_seq"] == 8
        assert got["fingerprint"]
        assert got["tool"] == "bash"

    def test_key_order_is_normalized(self):
        tid = "t_i3741_rep_order"
        _seed_messages(tid, [("tool_use", "bash", '{"a": 1, "b": 2}', 5),
                             ("tool_use", "bash", '{"b":2,"a":1}', 4)] * 4)
        got = self._call(tid, min_repeats=8)
        assert got is not None and got["repeats"] == 8

    def test_below_threshold_is_none(self):
        tid = "t_i3741_rep_below"
        _seed_messages(tid, _repeated_spec(7))
        assert self._call(tid) is None

    def test_outside_window_is_none(self):
        tid = "t_i3741_rep_old"
        _seed_messages(tid, _repeated_spec(8, ago=900))
        assert self._call(tid) is None

    def test_break_in_sequence_is_none(self):
        tid = "t_i3741_rep_break"
        specs = _repeated_spec(3, ago=6)
        specs.append(("tool_use", "bash", '{"command": "echo different"}', 5))
        specs.extend(_repeated_spec(3, ago=4))
        _seed_messages(tid, specs)
        # 尾部只有 3 条连续相同 → 未达 8
        assert self._call(tid) is None

    def test_different_tool_is_none(self):
        tid = "t_i3741_rep_tool"
        specs = _repeated_spec(4, tool="bash")
        specs.extend(_repeated_spec(4, tool="read", ago=4))
        _seed_messages(tid, specs)
        assert self._call(tid) is None


# ── 巡检 + 收口 ────────────────────────────────────────────────

class TestSweepStall:
    def test_finalize_when_retries_exhausted(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_TIMEOUT", "60")
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_RETRIES", "0")
        tid, seq, eid = "t_i3741_stall_final", SEQ_BASE + 1, SEQ_BASE + 1
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_messages(tid, _repeated_spec(8))
        _seed_exec(eid, tid, status="running")

        n = scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert n == 0  # 上限 0 → 不重派，直接收口

        row = _task_row(tid)
        assert row[0] == "failed"
        diag = _diag(row[2], "NO_PROGRESS_FAILED")
        assert diag is not None
        assert diag["reason"] == "repeated_step"
        assert diag["repeats"] == 8
        assert diag["first_seq"] == 1 and diag["last_seq"] == 8
        assert diag["step_fingerprint"]
        assert _exec_row(eid)[0] == "failed"
        assert "NO_PROGRESS" in (_exec_row(eid)[1] or "")
        assert _busy() == 0

    def test_requeue_then_finalize(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_TIMEOUT", "60")
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_RETRIES", "1")
        tid, seq = "t_i3741_stall_req", SEQ_BASE + 2
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_messages(tid, _repeated_spec(8))

        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        row = _task_row(tid)
        assert row[0] == "queued"
        assert row[1] is None  # 旧执行 superseded
        assert _diag(row[2], "NO_PROGRESS_REQUEUE")["reason"] == "repeated_step"

        # 模拟重派后重新认领：任务再次 running 且超阈值
        with db_lock:
            db.cursor().execute(
                "UPDATE agent_tasks SET status='running', started_at=? WHERE id=?",
                (_iso_ago(900), tid))
            db.commit()
        with db_lock:
            db.cursor().execute("UPDATE agent_runtimes SET busy_slots=1 WHERE id=?", (RT,))
            db.commit()

        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        row = _task_row(tid)
        assert row[0] == "failed"
        assert _diag(row[2], "NO_PROGRESS_FAILED") is not None

    def test_no_false_positive_for_distinct_commands(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_TIMEOUT", "60")
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_RETRIES", "0")
        tid, seq = "t_i3741_stall_ok", SEQ_BASE + 3
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_messages(tid, [
            ("tool_use", "bash", json.dumps({"command": f"echo step-{i}"}), 5)
            for i in range(10)])
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert _task_row(tid)[0] == "running"

    def test_disabled_switch_keeps_running(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_TIMEOUT", "60")
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_ENABLED", "0")
        tid, seq = "t_i3741_stall_off", SEQ_BASE + 4
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_messages(tid, _repeated_spec(8))
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        row = _task_row(tid)
        assert row[0] == "running"
        assert "NO_PROGRESS_DISABLED" in _codes(row[2])


# ── poll 超时主动收口 ──────────────────────────────────────────

class TestPollTimeoutTerminates:
    def test_timeout_terminates_active_task(self):
        tid, seq, eid = "t_i3741_poll", SEQ_BASE + 20, SEQ_BASE + 20
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_exec(eid, tid, status="dispatched")

        async def _run():
            return await scheduler._poll_dispatched_completion(
                eid, timeout=0.15, poll_interval=0.02)

        assert asyncio.run(_run()) == "failed"
        row = _task_row(tid)
        assert row[0] == "failed"
        assert "WORKER_TIMEOUT_TERMINATED" in _codes(row[2])
        assert _exec_row(eid)[0] == "failed"
        assert _busy() == 0

    def test_timeout_leaves_already_terminal_task(self):
        tid, seq, eid = "t_i3741_poll_done", SEQ_BASE + 21, SEQ_BASE + 21
        _seed_runtime(busy=0)
        _seed_task(tid, seq, status="failed", started_secs_ago=900)
        _seed_exec(eid, tid, status="dispatched")

        async def _run():
            return await scheduler._poll_dispatched_completion(
                eid, timeout=0.15, poll_interval=0.02)

        assert asyncio.run(_run()) == "failed"
        assert _task_row(tid)[0] == "failed"


# ── 反向残留 sweep ─────────────────────────────────────────────

class TestReverseZombieExec:
    def test_failed_exec_with_running_task_is_terminated(self):
        tid, seq, eid = "t_i3741_rev_fail", SEQ_BASE + 30, SEQ_BASE + 30
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_exec(eid, tid, status="failed", started_secs_ago=900, completed_secs_ago=600)

        n = scheduler._reap_terminal_exec_running_tasks()
        assert n >= 1
        row = _task_row(tid)
        assert row[0] == "failed"
        assert "TERMINAL_EXEC_RUNNING_TASK" in _codes(row[2])
        assert _busy() == 0

    def test_cancelled_exec_maps_to_cancelled_task(self):
        tid, seq, eid = "t_i3741_rev_cancel", SEQ_BASE + 31, SEQ_BASE + 31
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_exec(eid, tid, status="cancelled", started_secs_ago=900, completed_secs_ago=600)

        scheduler._reap_terminal_exec_running_tasks()
        assert _task_row(tid)[0] == "cancelled"

    def test_grace_period_and_terminal_task_untouched(self):
        tid1, seq1, eid1 = "t_i3741_rev_young", SEQ_BASE + 32, SEQ_BASE + 32
        _seed_runtime(busy=1)
        _seed_task(tid1, seq1, started_secs_ago=900)
        _seed_exec(eid1, tid1, status="failed", started_secs_ago=60, completed_secs_ago=10)

        tid2, seq2, eid2 = "t_i3741_rev_ok", SEQ_BASE + 33, SEQ_BASE + 33
        _seed_task(tid2, seq2, status="completed", started_secs_ago=900)
        _seed_exec(eid2, tid2, status="failed", started_secs_ago=900, completed_secs_ago=600)

        scheduler._reap_terminal_exec_running_tasks()
        assert _task_row(tid1)[0] == "running"     # 宽限内不动
        assert _task_row(tid2)[0] == "completed"   # 已终态不动
