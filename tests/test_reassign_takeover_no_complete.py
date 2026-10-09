"""换人 / bot 接管不再把需求置为已完成（docs/weekly-06/reassign-takeover-no-complete-design.md）。

覆盖：_close_other_assignee_tasks（换人，降级 no-op）、_close_issue_tasks_bot_takeover
（bot 接管，stage='done' 不再置 completed）。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.db_setup import init_database
from src.api.issues_center import _close_other_assignee_tasks, _close_issue_tasks_bot_takeover
from src.session_manager import db, db_lock

_RK = "gitcode:org/test"


def setup_module():
    init_database()


def _seed(issue_number, status, stage="vod", completed_reason=None):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (_RK, issue_number))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, stage, completed_reason) "
            "VALUES (1, 'owner', ?, ?, 't', ?, ?, ?)",
            (_RK, issue_number, status, stage, completed_reason))
        db.commit()


def _row(issue_number):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, stage, completed_reason FROM issue WHERE repo_key=? AND issue_number=?",
            (_RK, issue_number)).fetchone()


def _clean(issue_number):
    with db_lock:
        db.cursor().execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (_RK, issue_number))
        db.commit()


class TestReassignNoComplete:
    def test_reassign_does_not_complete(self):
        _seed(61, status="processing")
        assert _close_other_assignee_tasks(_RK, 61, "new_owner") == 0
        row = _row(61)
        assert row[0] == "processing"  # 不被标完成
        assert row[2] is None
        _clean(61)

    def test_reassign_idempotent_no_side_effects(self):
        _seed(62, status="active")
        assert _close_other_assignee_tasks(_RK, 62, "new_owner") == 0
        assert _row(62)[0] == "active"
        _clean(62)


class TestBotTakeoverNoComplete:
    def test_takeover_done_keeps_completed(self):
        """stage=done 且已 completed → 接管不改变（保持已完成，不翻回不重标）。"""
        _seed(63, status="completed", stage="done", completed_reason="review_passed")
        _close_issue_tasks_bot_takeover(_RK, 63)
        row = _row(63)
        assert row[0] == "completed"
        assert row[2] == "review_passed"
        _clean(63)

    def test_takeover_done_processing_not_completed(self):
        """stage=done 但 status=processing（异常态）→ 接管不置 completed。"""
        _seed(64, status="processing", stage="done")
        _close_issue_tasks_bot_takeover(_RK, 64)
        row = _row(64)
        assert row[0] != "completed"
        assert row[2] is None
        _clean(64)

    def test_takeover_active_no_running_pending_review(self):
        """活跃态且无 running agent → 仍置 pending_review（原语义保留，等开发者确认）。"""
        _seed(65, status="active", stage="vod")
        _close_issue_tasks_bot_takeover(_RK, 65)
        row = _row(65)
        assert row[0] == "pending_review"
        _clean(65)