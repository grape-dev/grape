"""反馈状态口径测试（Issue #673：user_feedback.status ↔ issue stage/status 映射与联动同步）。

覆盖：
- derive_feedback_status：stage / task_status 推导（#673/#22 共用口径）
- sync_feedback_statuses：已转换反馈状态联动更新；只升不降（不覆盖 admin 手工终态）
- 未转换（issue_url=''）/ 无 issue_id 的反馈不受同步影响
"""
from src.session_manager import db, db_lock
from src.api.db_setup import init_database

init_database()  # issue 表按需建（本用例组需要 issue 单表 join）

from src.api.feedback_status import (
    derive_feedback_status,
    sync_feedback_statuses,
    sync_external_feedback_statuses,
    FEEDBACK_STATUS_OPEN,
    FEEDBACK_STATUS_IN_PROGRESS,
    FEEDBACK_STATUS_RESOLVED,
    FEEDBACK_STATUS_CLOSED,
)
from unittest import mock
import src.api.feedback_status as fstat


def _insert_feedback(status="open", issue_url="", issue_id=None, source="user"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO user_feedback "
            "(user_id, username, category, title, description, repo_key, status, source, "
            "issue_id, issue_url, issue_number, is_deleted) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            (0, "u", "bug", "t", "d", "gitcode:o/r", status, source, issue_id,
             issue_url, "1" if issue_url else ""))
        db.commit()
        return c.lastrowid


def _insert_issue(stage="coding", status="active"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, stage) "
            "VALUES (0, '', 'gitcode:o/r', 100, 't', ?, ?)",
            (status, stage))
        db.commit()
        return c.lastrowid


def _cleanup_feedback(*fids):
    with db_lock:
        for f in fids:
            db.execute("DELETE FROM user_feedback WHERE id=?", (f,))
        db.commit()


def _cleanup_issue(iid):
    with db_lock:
        db.execute("DELETE FROM issue WHERE id=?", (iid,))
        db.commit()


class TestDeriveFeedbackStatus:
    def test_stage_map(self):
        assert derive_feedback_status("vod") == FEEDBACK_STATUS_OPEN
        assert derive_feedback_status("issue") == FEEDBACK_STATUS_OPEN
        assert derive_feedback_status("coding") == FEEDBACK_STATUS_IN_PROGRESS
        assert derive_feedback_status("qa") == FEEDBACK_STATUS_IN_PROGRESS
        assert derive_feedback_status("sre") == FEEDBACK_STATUS_IN_PROGRESS
        assert derive_feedback_status("done") == FEEDBACK_STATUS_RESOLVED
        assert derive_feedback_status("") == FEEDBACK_STATUS_OPEN
        assert derive_feedback_status(None) == FEEDBACK_STATUS_OPEN

    def test_task_status_overrides(self):
        assert derive_feedback_status("coding", "completed") == FEEDBACK_STATUS_RESOLVED
        assert derive_feedback_status("coding", "closed") == FEEDBACK_STATUS_CLOSED
        assert derive_feedback_status("done", "active") == FEEDBACK_STATUS_RESOLVED
        assert derive_feedback_status("done", "") == FEEDBACK_STATUS_RESOLVED


class TestSyncFeedbackStatuses:
    def test_sync_upgrades_status(self):
        iid = _insert_issue(stage="coding", status="active")
        fid = _insert_feedback(status="open", issue_url="u", issue_id=iid)
        try:
            updated = sync_feedback_statuses()
            assert updated >= 1
            with db_lock:
                st = db.execute("SELECT status FROM user_feedback WHERE id=?",
                                (fid,)).fetchone()[0]
            assert st == FEEDBACK_STATUS_IN_PROGRESS
        finally:
            _cleanup_feedback(fid)
            _cleanup_issue(iid)

    def test_sync_no_downgrade(self):
        iid = _insert_issue(stage="vod", status="active")
        fid = _insert_feedback(status="resolved", issue_url="u", issue_id=iid)
        try:
            sync_feedback_statuses()
            with db_lock:
                st = db.execute("SELECT status FROM user_feedback WHERE id=?",
                                (fid,)).fetchone()[0]
            assert st == "resolved"
        finally:
            _cleanup_feedback(fid)
            _cleanup_issue(iid)

    def test_sync_skips_unconverted(self):
        iid = _insert_issue(stage="done", status="completed")
        f1 = _insert_feedback(status="open", issue_url="", issue_id=iid)
        f2 = _insert_feedback(status="open", issue_url="u", issue_id=None)
        try:
            with db_lock:
                before = {r[0]: r[1] for r in db.execute(
                    "SELECT id, status FROM user_feedback WHERE id IN (?,?)",
                    (f1, f2)).fetchall()}
            sync_feedback_statuses()
            with db_lock:
                after = {r[0]: r[1] for r in db.execute(
                    "SELECT id, status FROM user_feedback WHERE id IN (?,?)",
                    (f1, f2)).fetchall()}
            assert before == after
        finally:
            _cleanup_feedback(f1, f2)
            _cleanup_issue(iid)


class TestSyncExternalFeedbackStatuses:
    """user 反馈外部 Issue 状态联动（Issue #673 QA S1 修复）。"""

    def test_closed_external_upgrades(self):
        fid = _insert_feedback(status="open", issue_url="https://gitcode.com/o/r/issues/9")
        try:
            with mock.patch.object(fstat, "_fetch_external_issue_state",
                                   return_value="closed"), \
                 mock.patch("src.api.feedback_issue_converter._repo_bot_token",
                            return_value="B"):
                updated = sync_external_feedback_statuses()
            assert updated >= 1
            with db_lock:
                st = db.execute("SELECT status FROM user_feedback WHERE id=?",
                                (fid,)).fetchone()[0]
            assert st == FEEDBACK_STATUS_CLOSED
        finally:
            _cleanup_feedback(fid)

    def test_open_external_no_downgrade(self):
        fid = _insert_feedback(status="resolved", issue_url="https://gitcode.com/o/r/issues/10")
        try:
            with mock.patch.object(fstat, "_fetch_external_issue_state",
                                   return_value="open"), \
                 mock.patch("src.api.feedback_issue_converter._repo_bot_token",
                            return_value="B"):
                sync_external_feedback_statuses()
            with db_lock:
                st = db.execute("SELECT status FROM user_feedback WHERE id=?",
                                (fid,)).fetchone()[0]
            assert st == "resolved"
        finally:
            _cleanup_feedback(fid)

    def test_skips_unconverted_and_mapped(self):
        """issue_url='' 或带内部 issue_id 映射的行不进外部拉取。"""
        unconverted = _insert_feedback(status="open", issue_url="")
        mapped = _insert_feedback(status="open", issue_url="u", issue_id=12345)
        try:
            with mock.patch.object(fstat, "_fetch_external_issue_state",
                                   return_value="closed") as m, \
                 mock.patch("src.api.feedback_issue_converter._repo_bot_token",
                            return_value="B"):
                sync_external_feedback_statuses()
            ids_fetched = [c[0][1] for c in m.call_args_list]
            assert unconverted not in ids_fetched
            assert mapped not in ids_fetched
        finally:
            _cleanup_feedback(unconverted, mapped)

    def test_closed_row_not_refetched(self):
        """已 closed 的行不再发起外部拉取（省 API 调用）。"""
        fid = _insert_feedback(status="closed", issue_url="https://gitcode.com/o/r/issues/11")
        try:
            with mock.patch.object(fstat, "_fetch_external_issue_state",
                                   return_value="closed") as m:
                sync_external_feedback_statuses()
            assert not m.called
        finally:
            _cleanup_feedback(fid)
