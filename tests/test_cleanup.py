"""M3 sweeper 清理测试（见 multi-issue-isolation-design.md §7.4）"""
import os
import pytest

from src.session_manager import db, db_lock
from src.runtime import cleanup


@pytest.fixture(autouse=True)
def _cleanup_env(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    with db_lock:
        for t in ("issue_state", "agent_tasks", "issue_agent_session"):
            db.cursor().execute(f"DELETE FROM {t}")
        db.commit()
    yield
    with db_lock:
        for t in ("issue_state", "agent_tasks", "issue_agent_session"):
            db.cursor().execute(f"DELETE FROM {t}")
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
