"""需求中心雷达「is_running」口径测试（local-engine-busy-slots-zombie-fix-design.md §8）。

对齐流水线点亮语义：只有任务真正认领执行（status='running'）才算进行中；
queued（排队未认领）/ dispatched（历史 pull 中间态）不算，避免雷达比流水线先亮。
"""
import pytest

from src.session_manager import db, db_lock
from src.api import issues_center as ic

RK = "gitcode:test/repo"


def _seed_task(tid: str, issue: str, status: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, agent_type, event_type, "
            "status, prompt, cwd, skill_files, env_extra) "
            "VALUES (?,?,?,?,?, 'vod', 'issue_opened', ?, 'x', '', '{}', '{}')",
            (tid, "sess-" + tid, 0, RK, issue, status))
        db.commit()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_ir%'")
        db.commit()


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


class TestIssueIsRunning:
    def test_queued_only_not_running(self):
        """排队未认领（#256 当时状态）：is_running=False。"""
        _seed_task("t_ir_q", "#1", "queued")
        assert ic._batch_running_agent_tasks([(RK, "#1")]) == set()

    def test_running_is_running(self):
        _seed_task("t_ir_r", "#1", "running")
        assert ic._batch_running_agent_tasks([(RK, "#1")]) == {(RK, "#1")}

    def test_dispatched_not_running(self):
        """dispatched（历史 pull 中间态）不作为进行中。"""
        _seed_task("t_ir_d", "#3", "dispatched")
        assert ic._batch_running_agent_tasks([(RK, "#3")]) == set()

    def test_mixed_returns_only_running(self):
        _seed_task("t_ira", "#1", "queued")
        _seed_task("t_irb", "#2", "running")
        _seed_task("t_irc", "#3", "dispatched")
        assert ic._batch_running_agent_tasks([(RK, "#1"), (RK, "#2"), (RK, "#3")]) == {(RK, "#2")}