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
