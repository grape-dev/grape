"""M3 sweeper 清理测试（见 multi-issue-isolation-design.md §7.4）"""
import os
import pytest

from datetime import datetime, timezone, timedelta

from src.session_manager import db, db_lock
from src.runtime import cleanup


def test_sweep_once_purges_stale_im_instances():
    """sweep_once 必须清理 24h 前的 IM 实例心跳行（bot_lease.purge_stale_instances）。

    回归背景：该钩子最初被放进 `if _has_db_tables:` 块里，而那个判断查的是 SQLite 的
    `sqlite_master` —— MySQL 后端恒为 False → 整块被跳过，清理永不执行（实测发现）。
    """
    _old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    _fresh = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_im_instance WHERE instance_id LIKE 'sweeptest-%'")
        c.execute("INSERT INTO grape_im_instance (instance_id, hostname, ip, last_heartbeat) "
                  "VALUES ('sweeptest-old','h','',?)", (_old,))
        c.execute("INSERT INTO grape_im_instance (instance_id, hostname, ip, last_heartbeat) "
                  "VALUES ('sweeptest-fresh','h','',?)", (_fresh,))
        db.commit()
    try:
        cleanup.sweep_once()
        with db_lock:
            ids = [r[0] for r in db.cursor().execute(
                "SELECT instance_id FROM grape_im_instance").fetchall()]
        assert "sweeptest-old" not in ids, "sweep_once 没有清理过期的 IM 实例心跳行"
        assert "sweeptest-fresh" in ids, "sweep_once 误删了新鲜心跳行"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM grape_im_instance WHERE instance_id LIKE 'sweeptest-%'")
            db.commit()


@pytest.fixture(autouse=True)
def _cleanup_env(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    with db_lock:
        for t in ("issue_state", "agent_tasks", "issue_agent_session"):
            try:
                db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
            except Exception:
                pass
        db.commit()
    yield
    with db_lock:
        for t in ("issue_state", "agent_tasks", "issue_agent_session"):
            try:
                db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
            except Exception:
                pass
        db.commit()


def _mk_workdir(repo_key, agent_type, num):
    from src.runtime import repogit
    d = repogit.issue_dir_for(repo_key, agent_type, num)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "scratch.txt"), "w", encoding="utf-8").write("x")
    return d


def _set_stage(repo_key, issue_id, stage):
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO issue_state (id, repo_key, issue_id, stage, owner_agent, updated_at) VALUES ((SELECT id FROM issue_state WHERE repo_key=? AND issue_id=?), ?, ?, ?, ?, datetime('now'))",
                  (repo_key, issue_id, repo_key, issue_id, stage, stage))
        db.commit()


def _set_active_issue_session(repo_key, agent_type, issue_id):
    from src.api.session_store import get_or_create_issue_session
    s = get_or_create_issue_session(repo_key, agent_type, issue_id)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, issue_session_id, status, prompt, issue_id, repo_key, agent_type) VALUES ('t-active', 'sess', 1, ?, 'running', 'p', ?, ?, ?)",
                  (s["id"], issue_id, repo_key, agent_type))
        db.commit()
    return s


class TestSweeper:
    def test_done_cleans_workdir(self):
        _set_stage("gitcode:org/test", "#5", "done")
        d = _mk_workdir("gitcode:org/test", "coding", "5")
        assert os.path.isdir(d)
        cleanup.sweep_once()
        assert not os.path.isdir(d), "done 后 workdir 应被清理"

    def test_active_stage_not_cleaned(self):
        _set_stage("gitcode:org/test", "#5", "coding")
        d = _mk_workdir("gitcode:org/test", "coding", "5")
        cleanup.sweep_once()
        assert os.path.isdir(d), "coding 进行中的 workdir 不得清理"

    def test_running_session_skipped(self):
        _set_stage("gitcode:org/test", "#5", "done")
        s = _set_active_issue_session("gitcode:org/test", "coding", "#5")
        _mk_workdir("gitcode:org/test", "coding", "5")
        cleanup.sweep_once()
        assert os.path.isdir(repogit_dir("gitcode:org/test", "coding", "5")), "有 running 任务的会话不清理"


def repogit_dir(repo_key, agent_type, num):
    from src.runtime import repogit
    return repogit.issue_dir_for(repo_key, agent_type, num)


class TestStuckEventText:
    """sweeper 文案区分「创建任务失败」与「running 超时」——agent-tasks-direct-create-binding-fix-design.md §5.4"""

    def _mk_running_event(self, seq, repo_key, started_at):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, status, started_at) "
                "VALUES (?, '?#0', 'issue_opened', 'vod', 't', 'running', ?)",
                (repo_key, started_at))
            db.commit()
            return c.lastrowid

    def test_no_task_row_marks_create_failure(self):
        repo_key = "gitcode:org/sweep-text-1"
        seq = self._mk_running_event(1, repo_key, "2020-01-01T00:00:00+00:00")
        cleanup.sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        with db_lock:
            row = db.cursor().execute(
                "SELECT status, result FROM events WHERE seq=?", (seq,)).fetchone()
        assert row and row[0] == "failed"
        assert "创建任务失败" in row[1] and "create_direct_task" in row[1]

    def test_has_task_row_marks_timeout(self):
        repo_key = "gitcode:org/sweep-text-2"
        seq = self._mk_running_event(2, repo_key, "2020-01-01T00:00:00+00:00")
        with db_lock:
            db.cursor().execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, status, prompt, repo_key, agent_type) "
                "VALUES ('t-sweep-timeout', 'sess', 0, ?, 'completed', 'p', ?, 'vod')", (seq, repo_key))
            db.commit()
        cleanup.sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        with db_lock:
            row = db.cursor().execute(
                "SELECT status, result FROM events WHERE seq=?", (seq,)).fetchone()
        assert row and row[0] == "failed"
        assert "running 超时" in row[1] and "创建任务失败" not in row[1]


_OLD = "2020-01-01T00:00:00+00:00"


def _mk_event(seq, repo_key, started_at=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, status, started_at) "
            "VALUES (?, '#1', 'issue_opened', 'vod', 't', 'running', ?)",
            (repo_key, started_at))
        db.commit()
        return c.lastrowid


class TestStuckAgentTasks:
    """sweeper 回收悬空 agent_tasks —— 需求中心「运行中/雷达扫描」亮灯根因（#210 佐证）。

    现状缺口：sweep_stuck_events_once 只回收 events 不碰 agent_tasks；
    广播 queued 无 worker 认领、runtime 已死的 running/dispatched 永远悬空。
    """

    @pytest.fixture(autouse=True)
    def _clean(self):
        with db_lock:
            for t in ("agent_tasks", "events", "agent_runtimes"):
                try:
                    db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
                except Exception:
                    pass
            db.commit()
        yield
        with db_lock:
            for t in ("agent_tasks", "events", "agent_runtimes"):
                try:
                    db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
                except Exception:
                    pass
            db.commit()

    def _mk_task(self, tid, status, runtime_id=0, created_at=None, started_at=None, event_seq=None,
                 repo_key="gitcode:org/sweep", stream_ended=0, self_reported=0):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, status, prompt, "
                "created_at, started_at, repo_key, issue_id, agent_type, stream_ended, self_reported) "
                "VALUES (?, 'sess', ?, ?, ?, 'p', ?, ?, ?, '#1', 'vod', ?, ?)",
                (tid, runtime_id, event_seq, status,
                 created_at or datetime.now(timezone.utc).isoformat(),
                 started_at, repo_key, stream_ended, self_reported))
            db.commit()

    def _mk_runtime(self, rid, status):
        with db_lock:
            db.cursor().execute(
                "INSERT INTO agent_runtimes (id, name, status, credential_id) VALUES (?, 'rt', ?, 0)", (rid, status))
            db.commit()

    def _status(self, tid):
        with db_lock:
            row = db.cursor().execute("SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        return row[0] if row else None

    def test_queued_stale_recycled(self):
        self._mk_task("t-q", "queued", created_at=_OLD)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        assert self._status("t-q") == "failed"

    def test_queued_fresh_kept(self):
        self._mk_task("t-q-fresh", "queued")
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        assert self._status("t-q-fresh") == "queued"

    def test_queued_stale_finalizes_running_event(self):
        seq = _mk_event(7001, "gitcode:org/sweep")
        self._mk_task("t-q", "queued", event_seq=seq, created_at=_OLD)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        with db_lock:
            ev = db.cursor().execute("SELECT status, result FROM events WHERE seq=?", (seq,)).fetchone()
        assert self._status("t-q") == "failed"
        assert ev and ev[0] == "failed" and "queued" in ev[1]

    def test_queued_stale_does_not_clobber_completed_event(self):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, status) "
                "VALUES ('gitcode:org/sweep', '#1', 'issue_opened', 'vod', 't', 'completed')")
            seq = c.lastrowid
            db.commit()
        self._mk_task("t-q", "queued", event_seq=seq, created_at=_OLD)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        with db_lock:
            ev = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
        assert self._status("t-q") == "failed"
        assert ev[0] == "completed", "已完成事件不得被回写"

    def test_running_dead_runtime_recycled(self):
        self._mk_task("t-dead", "running", runtime_id=999, started_at=_OLD)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        assert self._status("t-dead") == "failed"

    def test_running_runtime_id_zero_kept(self):
        # 本机 direct 自执行：runtime_id=0 + status=running（started_at 常为空）→ 绝不判死
        self._mk_task("t-zero", "running", runtime_id=0, started_at=None, created_at=_OLD)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        assert self._status("t-zero") == "running"

    def test_running_online_runtime_kept(self):
        # runtime 在线且 started_at 仅 60min 前（>30min runtime 死亡阈值但远未达 6h 长超时）→ 不回收
        self._mk_runtime(24, "online")
        self._mk_task("t-online", "running", runtime_id=24,
                      started_at=(datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat())
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=30,
                                             long_running_minutes=360)
        assert self._status("t-online") == "running"

    def test_running_offline_runtime_recycled(self):
        self._mk_runtime(25, "offline")
        self._mk_task("t-offline", "running", runtime_id=25, started_at=_OLD)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        assert self._status("t-offline") == "failed"

    def test_running_long_stale_recycled_even_online(self):
        # runtime 在线但 running 超长未收敛（>6h，worker 任务超时 1h）也判死（AgentLens #37-39 情形）
        self._mk_runtime(24, "online")
        self._mk_task("t-long", "running", runtime_id=24, started_at=_OLD)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10,
                                             long_running_minutes=360)
        assert self._status("t-long") == "failed"

    def test_running_online_recent_kept(self):
        # started_at 新鲜（在线 runtime）→ 不回收
        self._mk_runtime(24, "online")
        self._mk_task("t-recent", "running", runtime_id=24,
                      started_at=datetime.now(timezone.utc).isoformat())
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10,
                                             long_running_minutes=360)
        assert self._status("t-recent") == "running"

    def test_running_no_started_at_kept(self):
        # started_at 为空（无法按年龄判定，保守跳过）不误杀
        self._mk_task("t-nostart", "running", runtime_id=999, started_at=None, created_at=_OLD)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        assert self._status("t-nostart") == "running"

    def test_dual_signal_stuck_recycled_even_fresh(self):
        # 双信号齐（stream_ended+self_reported）但任务仍非终态 → 收敛失败残留，无年龄要求直接回收
        # 实例：#214（issue #10）QA 任务 t_<task>：事件 2219 已 completed、output 齐全，任务恒 running
        self._mk_task("t-dual", "running", runtime_id=0, started_at=None,
                      created_at=datetime.now(timezone.utc).isoformat(),
                      stream_ended=1, self_reported=1)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        with db_lock:
            row = db.cursor().execute(
                "SELECT status, result_text FROM agent_tasks WHERE id='t-dual'").fetchone()
        assert row and row[0] == "failed"
        assert "双信号" in row[1]

    def test_dual_signal_queued_stuck_recycled(self):
        self._mk_task("t-dual-q", "queued", runtime_id=0, started_at=None,
                      created_at=datetime.now(timezone.utc).isoformat(),
                      stream_ended=1, self_reported=1)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        assert self._status("t-dual-q") == "failed"

    def test_running_single_signal_kept(self):
        # 仅一个信号（self_reported=1 但 stream_ended=0，或反之）→ 仍在等另一信号，不回收
        self._mk_task("t-one", "running", runtime_id=0, started_at=None,
                      created_at=_OLD, stream_ended=1, self_reported=0)
        cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=10, dead_runtime_minutes=10)
        assert self._status("t-one") == "running"

    def test_sweep_once_integrates_agent_tasks_recycle(self):
        seq = _mk_event(7002, "gitcode:org/sweep")
        self._mk_task("t-q", "queued", event_seq=seq, created_at=_OLD)
        cleanup.sweep_once()
        with db_lock:
            ev = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
        assert self._status("t-q") == "failed"
        assert ev[0] == "failed"
