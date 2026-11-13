"""接管标记（takeover-mark）回归测试。

背景：点接管时前端会 cancel 原任务，cancel 把关联事件级联成 cancelled，
流水线于是渲染成「已取消 / 中断」。但对人的视角来说接管意味着这个 Issue
仍有人在推进（本地 dsh 会话），状态应该显示「进行中」。

修法：新增 event 状态 `takeover`，由 pipeline_status 映射成 STATUS_RUNNING
供展示；服务端不写 running，因为 runner 已经退出，声称 running 会误导
claim / 串行槽 / 超时判断。
"""
from datetime import datetime, timezone

import pytest

from src.api.pipeline_status import EVENT_STATUS_MAP, STATUS_CANCELLED, STATUS_RUNNING
from src.domains.runtime.engine import service
from src.kernel.session_manager import db, db_lock

REPO = "gitcode:testtakeover/repo"
ISSUE = "#17"


@pytest.fixture(autouse=True)
def _bind_db():
    """绑定到被测 service 模块的连接（同 takeover 测试：防全树收集时句柄陈旧）。"""
    global db, db_lock
    from src.domains.runtime.engine import service as _svc
    db, db_lock = _svc.db, _svc.db_lock


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id LIKE 'task-takeover-%'")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:testtakeover/%'")
            db.commit()

    wipe()
    yield
    wipe()


def _insert_event(seq, status="running"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status, agent_type, issue_id, "
            "scene_id, created_at, completed_at) VALUES (?, ?, 'a2a_event', ?, 'coding', ?, '', ?, ?)",
            (seq, REPO, status, ISSUE,
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat() if status in ("cancelled", "completed", "failed") else ""),
        )
        db.commit()


def _insert_task(task_id, event_seq, status="running"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, created_at) "
            "VALUES (?, ?, 0, ?, ?, ?, 'coding', 'p', ?, ?)",
            (task_id, f"sess-{task_id}", event_seq, ISSUE, REPO, status,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()


def _event_status(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM events WHERE seq=?", (seq,))
        row = c.fetchone()
    return row[0] if row else None


def _event_completed_at(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT completed_at FROM events WHERE seq=?", (seq,))
        row = c.fetchone()
    return row[0] if row else None


class TestTakeoverMark:
    def test_cancelled_event_flips_to_takeover(self):
        """核心场景：cancel 后事件是 cancelled，接管标记要把它拉回 takeover。"""
        _insert_event(7701, status="cancelled")
        _insert_task("task-takeover-1", 7701, status="cancelled")

        res = service.mark_task_takeover("task-takeover-1")

        assert res["status"] == "marked"
        assert res["before"] == "cancelled"
        assert res["after"] == "takeover"
        assert _event_status(7701) == "takeover"

    def test_completed_at_is_cleared(self):
        """cancel 时写过 completed_at；接管中的事件不该带着完成时间。"""
        _insert_event(7702, status="cancelled")
        _insert_task("task-takeover-2", 7702, status="cancelled")
        assert _event_completed_at(7702) not in (None, "")

        service.mark_task_takeover("task-takeover-2")

        assert _event_completed_at(7702) in (None, "")

    def test_idempotent(self):
        """重复标记不报错，状态保持 takeover。"""
        _insert_event(7703, status="cancelled")
        _insert_task("task-takeover-3", 7703, status="cancelled")

        first = service.mark_task_takeover("task-takeover-3")
        second = service.mark_task_takeover("task-takeover-3")

        assert first["status"] == second["status"] == "marked"
        assert _event_status(7703) == "takeover"

    def test_unknown_task(self):
        res = service.mark_task_takeover("task-takeover-does-not-exist")
        assert res["status"] == "not_found"

    def test_task_without_event(self):
        _insert_task("task-takeover-noevent", 0, status="cancelled")
        res = service.mark_task_takeover("task-takeover-noevent")
        assert res["status"] == "no_event"

    def test_does_not_touch_other_events(self):
        """只动自己的关联事件，别的 Issue 事件不受影响。"""
        _insert_event(7704, status="cancelled")
        _insert_task("task-takeover-4", 7704, status="cancelled")
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO events (seq, repo_key, event_type, status, agent_type, issue_id, "
                "scene_id, created_at) VALUES (?, 'gitcode:testtakeover/other', 'a2a_event', "
                "'cancelled', 'qa', '#99', '', ?)",
                (7705, datetime.now(timezone.utc).isoformat()),
            )
            db.commit()

        service.mark_task_takeover("task-takeover-4")

        assert _event_status(7704) == "takeover"
        assert _event_status(7705) == "cancelled"


class TestPipelineStatusMapping:
    def test_takeover_maps_to_running_for_display(self):
        """流水线展示层把 takeover 当「进行中」。"""
        assert EVENT_STATUS_MAP["takeover"] == STATUS_RUNNING

    def test_cancelled_stays_cancelled(self):
        """cancelled 的映射没被改动。"""
        assert EVENT_STATUS_MAP["cancelled"] == STATUS_CANCELLED

    def test_takeover_and_running_are_visually_identical(self):
        assert EVENT_STATUS_MAP["takeover"] == EVENT_STATUS_MAP["running"]