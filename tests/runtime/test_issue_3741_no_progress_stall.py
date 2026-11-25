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
              "RUNTIME_NO_PROGRESS_REPEAT_WINDOW",
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


# ── PR #2160 复审 ①：窗口与阈值解耦（115s/步真实节奏） ──────────

def _cadence_spec(n=8, step_secs=115, tool="bash",
                  inp='{"command": "sleep 115; tail -5 /tmp/x.log; pgrep -f act.py"}',
                  tail_ago=5):
    """按真实节奏（每步 step_secs 秒）生成 n 条相同指纹的 tool_use，最新一条在 tail_ago。"""
    return [("tool_use", tool, inp, tail_ago + (n - 1 - i) * step_secs)
            for i in range(n)]


class TestRepeatWindowConfig:
    def test_default_is_auto(self):
        assert tt.no_progress_repeat_window_sec({"skill": "s", "repo_key": "r"},
                                                cfg={}) == 0

    def test_top_level_and_task_env_override(self):
        t = {"skill": "s", "repo_key": "r"}
        assert tt.no_progress_repeat_window_sec(t, cfg={"no_progress_repeat_window_sec": 1200}) == 1200
        t_env = {"skill": "s", "repo_key": "r",
                 "env_extra": json.dumps({"RUNTIME_NO_PROGRESS_REPEAT_WINDOW": "1500"})}
        assert tt.no_progress_repeat_window_sec(t_env, cfg={"no_progress_repeat_window_sec": 1200}) == 1500

    def test_per_skill_and_bad_value(self):
        cfg = {"per_skill": {"s": {"no_progress_repeat_window_sec": 300}}}
        assert tt.no_progress_repeat_window_sec({"skill": "s", "repo_key": "r"}, cfg=cfg) == 300
        t = {"skill": "s", "repo_key": "r"}
        assert tt.no_progress_repeat_window_sec(t, cfg={"no_progress_repeat_window_sec": -5}) == 0
        assert tt.no_progress_repeat_window_sec(t, cfg={"no_progress_repeat_window_sec": "bad"}) == 0


class TestRepeatWindowResolve:
    def test_auto_covers_steps_at_115s_cadence(self):
        # 600s 阈值 ÷ 115s/步 ≈ 5 步 → 8 步阈值必须靠窗口下界兜住
        assert scheduler._no_progress_repeat_window(600, 8) == 8 * 120

    def test_threshold_wins_when_larger(self):
        assert scheduler._no_progress_repeat_window(6000, 8) == 6000

    def test_explicit_window_wins(self):
        assert scheduler._no_progress_repeat_window(600, 8, 1200) == 1200


class TestRealCadenceDetection:
    """主场景回归：115s/步、8 步同命令，默认 600s 阈值下必须命中。"""

    def test_probe_geometry_old_window_misses(self):
        tid = "t_i3741_cad_probe"
        _seed_messages(tid, _cadence_spec(8))
        with db_lock:
            c = db.cursor()
            # 旧口径（窗口 = 阈值 600s）：窗口内只有 ~6 步，8 步阈值必然 miss
            assert ta.repeated_tool_steps(c, tid, 600, 8, now_epoch=time.time()) is None
            # 新口径（窗口与阈值解耦）：8 步全部落在窗口内 → 命中
            got = ta.repeated_tool_steps(c, tid, 960, 8, now_epoch=time.time())
        assert got is not None and got["repeats"] == 8

    def test_sweep_detects_at_default_threshold(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_RETRIES", "0")
        tid, seq, eid = "t_i3741_cad_sweep", SEQ_BASE + 40, SEQ_BASE + 40
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=1200)   # 默认阈值 600s
        _seed_messages(tid, _cadence_spec(8))
        _seed_exec(eid, tid, status="running")

        scheduler._sweep_no_progress_tasks(now_epoch=time.time())

        row = _task_row(tid)
        assert row[0] == "failed"
        diag = _diag(row[2], "NO_PROGRESS_FAILED")
        assert diag is not None
        assert diag["reason"] == "repeated_step"
        assert diag["repeats"] == 8
        assert diag["repeat_threshold"] == 8
        assert diag["repeat_window"] == 960        # 窗口与 600s 阈值解耦
        assert diag["first_seq"] == 1 and diag["last_seq"] == 8
        assert _busy() == 0


# ── PR #2160 复审 ②：retries=0 语义与 EXHAUSTED 诊断自洽 ────────

class TestRetriesZeroSemantics:
    def test_zero_finalizes_and_diagnostic_is_consistent(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_TIMEOUT", "60")
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_RETRIES", "0")
        tid, seq = "t_i3741_zero_sem", SEQ_BASE + 41
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_messages(tid, _repeated_spec(8))

        scheduler._sweep_no_progress_tasks(now_epoch=time.time())

        row = _task_row(tid)
        assert row[0] == "failed"                  # 0 = 不重派、命中即收口
        assert _diag(row[2], "NO_PROGRESS_REQUEUE") is None
        exhaust = _diag(row[2], "NO_PROGRESS_EXHAUSTED")
        final = _diag(row[2], "NO_PROGRESS_FAILED")
        assert exhaust is not None and final is not None
        # 两条诊断不得互相矛盾：EXHAUSTED 不能再声称「交给原失败路径」
        assert "finalized as failed" in exhaust["note"]
        assert "original failure path" not in exhaust["note"]
        assert final["reason"] == "repeated_step"


# ── PR #2160 复审 ③：新增终止路径受 no_progress_enabled 门禁 ────

class TestTerminationGates:
    def _disabled_task(self, tid, seq, **kw):
        _seed_task(tid, seq, env_extra=json.dumps({"RUNTIME_NO_PROGRESS_ENABLED": "0"}), **kw)

    def test_poll_timeout_skips_terminate_when_disabled(self):
        tid, seq, eid = "t_i3741_gate_poll", SEQ_BASE + 50, SEQ_BASE + 50
        _seed_runtime(busy=1)
        self._disabled_task(tid, seq, started_secs_ago=900)
        _seed_exec(eid, tid, status="dispatched")

        async def _run():
            return await scheduler._poll_dispatched_completion(
                eid, timeout=0.15, poll_interval=0.02)

        assert asyncio.run(_run()) == "failed"     # exec 仍按原语义 failed
        row = _task_row(tid)
        assert row[0] == "running"                 # 任务不动（整体回滚）
        assert "WORKER_TIMEOUT_TERMINATED" not in _codes(row[2])
        assert _busy() == 1

    def test_reverse_sweep_skips_when_disabled(self):
        tid, seq, eid = "t_i3741_gate_rev", SEQ_BASE + 51, SEQ_BASE + 51
        _seed_runtime(busy=1)
        self._disabled_task(tid, seq, started_secs_ago=900)
        _seed_exec(eid, tid, status="failed", started_secs_ago=900, completed_secs_ago=600)

        assert scheduler._reap_terminal_exec_running_tasks() == 0
        row = _task_row(tid)
        assert row[0] == "running"
        assert "TERMINAL_EXEC_RUNNING_TASK" not in _codes(row[2])
        assert _busy() == 1


# ── PR #2160 复审 ⑤：started_at 乐观条件 / 指标归因 ─────────────

class TestOptimisticStartedAtGuard:
    def test_terminate_skips_when_execution_superseded(self):
        tid, seq = "t_i3741_guard_term", SEQ_BASE + 60
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        stale = _task_row(tid)[1]

        ok = scheduler._terminate_stuck_agent_task(
            tid, "stale execution", expected_started_at=stale + "0")

        assert ok is False
        row = _task_row(tid)
        assert row[0] == "running"
        # 旧执行的诊断不得串到新执行上（守卫在终态/大字段写入之前就短路）
        assert "TASK_TERMINATED_STUCK" not in _codes(row[2])
        assert _busy() == 1

    def test_finalize_skips_when_started_at_changed(self):
        tid, seq = "t_i3741_guard_final", SEQ_BASE + 61
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        stale = _task_row(tid)[1]

        diag = {"error_code": "NO_PROGRESS_FAILED", "task_id": tid,
                "reason": "repeated_step", "repeats": 8, "first_seq": 1, "last_seq": 8,
                "tool": "bash", "step_fingerprint": "deadbeef"}
        assert scheduler._finalize_no_progress(tid, stale + "0", diag) is False
        row = _task_row(tid)
        assert row[0] == "running"
        assert "NO_PROGRESS_FAILED" not in _codes(row[2])
        assert _busy() == 1

    def test_reverse_sweep_metric_carries_runtime_id(self, monkeypatch):
        import src.domains.insight.public as insight_public
        seen = []

        def _spy(task_id, source, runtime_id, failure_reason=""):
            seen.append((task_id, source, runtime_id, failure_reason))

        monkeypatch.setattr(insight_public, "record_task_failed", _spy)
        tid, seq, eid = "t_i3741_metric_rt", SEQ_BASE + 62, SEQ_BASE + 62
        _seed_runtime(busy=1)
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_exec(eid, tid, status="failed", started_secs_ago=900, completed_secs_ago=600)

        scheduler._reap_terminal_exec_running_tasks()

        assert _task_row(tid)[0] == "failed"
        assert seen and seen[0][2] == RT            # 不再写 0（保留引擎归因）
        assert seen[0][3] == "terminal_exec_running_task"
