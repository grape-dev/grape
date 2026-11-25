"""Issue #4340: 停机汇总行计数口径——候选数不得冒充失败数。

缺陷（生产 2.3.0a1）：`terminate_active_agents` 汇总行用 `len(stale_tasks)`（候选总数）
输出 `marked N stale task(s) as failed`。REMOTE 任务（worker 承载，credential_id>0）走
`pending_self_report` 不计失败，REMOTE-only 窗口因此虚报（生产 9/9 全 REMOTE、真实 failed=0）。

覆盖：
1. REMOTE-only：`failed_direct=0` / `pending_self_report=N`，且全程无 `as failed` 汇总文案；
2. DIRECT-only：`failed_direct=N`，与 `agent_tasks.status='failed'` 条数相等；
3. 混合：`candidates = failed_direct + pending_self_report`；
4. 竞态：SELECT 与 UPDATE 之间任务被并发自报推进到终态 → UPDATE 命中 0 行，
   `failed_direct` 不增加、已终态任务不被覆写成 failed；
5. 无候选：不输出汇总行。
"""
import logging
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.kernel.session_manager import db, db_lock

RT_DIRECT = 9431
RT_REMOTE = 9432
CRED_REMOTE = 9542
RK = "gitcode:shutdown/test4340"
SUMMARY_PREFIX = "terminate_active_agents: shutdown sweep"
FAILED_TEXT = "stale task(s) as failed"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "agent_runtimes", "events", "runtime_credentials"):
            try:
                c.execute(f"DELETE FROM {t} WHERE runtime_id IN ({RT_DIRECT},{RT_REMOTE}) "
                          "OR id LIKE 't_i4340%' OR repo_key=?", (RK,))
            except Exception:
                pass
        try:
            c.execute("DELETE FROM events WHERE seq >= 9740 AND seq < 9760")
        except Exception:
            pass
        db.commit()


def _seed_runtime(rid, cid, busy=1):
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        if "busy_slots" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN busy_slots INTEGER DEFAULT 0")
        if "max_concurrent" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN max_concurrent INTEGER DEFAULT 4")
        c.execute("INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
                  "busy_slots, max_concurrent) VALUES (?,?,?,'online',1,1,?,4)",
                  (rid, f"rt-{rid}", cid, busy))
        db.commit()


def _seed_task(tid, rid, seq, status="running"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, "
            "agent_type, event_type, status, prompt, cwd, skill_files, env_extra, event_seq) "
            "VALUES (?,?,?,?,?, 'vod', 'a2a_event', ?, 'x', '', '{}', '{}', ?)",
            (tid, "sess-" + tid, rid, RK, "#4340", status, seq))
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute("INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status, "
                  "summary, result) VALUES (?,?,?,?,?, 'running', 'A2A: → coding', '')",
                  (seq, RK, "#4340", "a2a_event", "vod"))
        db.commit()


def _task_row(tid):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, exit_code, errors FROM agent_tasks WHERE id=?", (tid,)).fetchone()


def _event_status(seq):
    with db_lock:
        return db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()[0]


def _failed_rows():
    with db_lock:
        return db.cursor().execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE repo_key=? AND status='failed'", (RK,)).fetchone()[0]


class _RacingCursor:
    """代理游标：在判死 / 置 pending 的 UPDATE 之前把目标任务翻成终态，模拟并发自报竞态。"""

    _INTERCEPT = ("UPDATE agent_tasks SET status='failed'",
                  "UPDATE agent_tasks SET status='pending_self_report'")

    def __init__(self, real, flip_ids):
        self._real = real
        self._flip_ids = flip_ids

    def execute(self, sql, params=()):
        flat = " ".join(str(sql).split())
        if flat.startswith(self._INTERCEPT) and params:
            tid = params[-1]
            if tid in self._flip_ids:
                self._flip_ids.discard(tid)
                self._real.execute(
                    "UPDATE agent_tasks SET status='completed', exit_code=0 WHERE id=?", (tid,))
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _RacingDB:
    def __init__(self, real, flip_ids):
        self._real = real
        self._flip_ids = set(flip_ids)

    def cursor(self):
        return _RacingCursor(self._real.cursor(), self._flip_ids)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run_sweep(caplog):
    from src.domains.runtime.agents.executor import terminate_active_agents
    caplog.set_level(logging.INFO, logger="devops-bot")
    caplog.clear()
    terminate_active_agents(grace_seconds=0)
    return [r.getMessage() for r in caplog.records]


def _summary(msgs):
    hit = [m for m in msgs if SUMMARY_PREFIX in m]
    assert len(hit) == 1, f"应输出且仅输出一条汇总行，实际 {hit}"
    return hit[0]


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


class TestSweepCounts:
    def test_remote_only_reports_no_failure(self, caplog):
        """REMOTE-only：failed_direct=0 / pending_self_report=N，且不含 as failed 文案。"""
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        for i in range(3):
            _seed_task(f"t_i4340_remote_{i}", RT_REMOTE, 9740 + i)
        msgs = _run_sweep(caplog)
        line = _summary(msgs)
        assert "candidates=3" in line
        assert "failed_direct=0" in line
        assert "pending_self_report=3" in line
        assert not [m for m in msgs if FAILED_TEXT in m], "REMOTE-only 窗口不得出现 as failed 汇总文案"
        for i in range(3):
            assert _task_row(f"t_i4340_remote_{i}")[0] == "pending_self_report"

    def test_direct_only_counts_real_failures(self, caplog):
        """DIRECT-only：failed_direct 与 agent_tasks.status='failed' 条数相等。"""
        _seed_runtime(RT_DIRECT, 0)
        for i in range(2):
            _seed_task(f"t_i4340_direct_{i}", RT_DIRECT, 9743 + i)
        msgs = _run_sweep(caplog)
        line = _summary(msgs)
        assert "candidates=2" in line
        assert "failed_direct=2" in line
        assert "pending_self_report=0" in line
        assert [m for m in msgs if "marked 2 " in m and FAILED_TEXT in m], "真失败时保留旧语义文案"
        assert _failed_rows() == 2

    def test_mixed_candidates_split(self, caplog):
        """混合：candidates = failed_direct + pending_self_report。"""
        _seed_runtime(RT_DIRECT, 0)
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i4340_mix_direct", RT_DIRECT, 9745)
        _seed_task("t_i4340_mix_remote_a", RT_REMOTE, 9746)
        _seed_task("t_i4340_mix_remote_b", RT_REMOTE, 9747)
        msgs = _run_sweep(caplog)
        line = _summary(msgs)
        assert "candidates=3" in line
        assert "failed_direct=1" in line
        assert "pending_self_report=2" in line
        assert "marked 1 " in " ".join(m for m in msgs if FAILED_TEXT in m)
        assert _failed_rows() == 1
        assert _task_row("t_i4340_mix_direct")[0] == "failed"
        assert _task_row("t_i4340_mix_remote_a")[0] == "pending_self_report"

    def test_race_zero_row_not_counted(self, caplog, monkeypatch):
        """竞态：任务在 SELECT 与 UPDATE 之间自报终态 → 命中 0 行，不计 failed_direct。"""
        import src.kernel.session_manager as sm
        _seed_runtime(RT_DIRECT, 0)
        _seed_task("t_i4340_race", RT_DIRECT, 9748)
        monkeypatch.setattr(sm, "db", _RacingDB(sm.db, {"t_i4340_race"}))
        msgs = _run_sweep(caplog)
        line = _summary(msgs)
        assert "candidates=1" in line
        assert "failed_direct=0" in line
        assert "pending_self_report=0" in line
        assert not [m for m in msgs if FAILED_TEXT in m], "0 行竞态不得计入失败数"
        row = _task_row("t_i4340_race")
        assert row[0] == "completed", "已终态任务不得被停机判死覆写"
        assert row[1] == 0
        assert _event_status(9748) == "running", "竞态行不写事件终态（由自报方收口）"

    def test_race_remote_zero_row_not_counted(self, caplog, monkeypatch):
        """竞态：REMOTE 任务在置 pending 前已自报终态 → 不计 pending_self_report。"""
        import src.kernel.session_manager as sm
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i4340_race_remote", RT_REMOTE, 9749)
        monkeypatch.setattr(sm, "db", _RacingDB(sm.db, {"t_i4340_race_remote"}))
        msgs = _run_sweep(caplog)
        line = _summary(msgs)
        assert "candidates=1" in line
        assert "failed_direct=0" in line
        assert "pending_self_report=0" in line
        assert not [m for m in msgs if FAILED_TEXT in m]
        row = _task_row("t_i4340_race_remote")
        assert row[0] == "completed", "已终态任务不得被置 pending_self_report"
        assert _event_status(9749) == "running"

    def test_no_candidates_no_summary(self, caplog):
        """无运行中任务时不输出汇总行（保留原行为）。"""
        _seed_runtime(RT_DIRECT, 0, busy=0)
        msgs = _run_sweep(caplog)
        assert not [m for m in msgs if SUMMARY_PREFIX in m]
        assert not [m for m in msgs if FAILED_TEXT in m]