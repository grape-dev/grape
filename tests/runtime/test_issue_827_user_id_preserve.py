"""Issue #827: 跨流水线移交后 user_id 被清空回归测试。

覆盖：
1. ensure_issue_task assignee 变更为未注册用户时，已有 user_id 不被清零。
2. ensure_issue_task 建卡时写入 created_by（创建人 GitCode 用户名）。
3. ensure_issue_task 复用时回补空 created_by。
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")

REPO = "gitcode:owner827/demo"
MAPPED = "mapped_user_827"
UNMAPPED = "unmapped_ghost_827"
CREATOR_UID = None


def _db():
    from src.kernel.session_manager import db, db_lock
    return db, db_lock


def _clean():
    db, db_lock = _db()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN (?, ?)", (MAPPED, UNMAPPED))
        c.execute("DELETE FROM grape_accounts WHERE username=?", (MAPPED,))
        db.commit()


def setup_module():
    from src.api import db_setup
    try:
        db_setup.init_database()
    except Exception:
        pass
    global CREATOR_UID
    from src.kernel.session_manager import db, db_lock
    from src.domains.mgmt.auth.password import hash_password
    _clean()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                  "VALUES (?, NULL, ?, ?, 1, 0)", (MAPPED, hash_password("x"), MAPPED))
        CREATOR_UID = c.lastrowid
        c.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user, updated_at) "
                  "VALUES (?, ?, datetime('now'))", (CREATOR_UID, MAPPED))
        db.commit()


def teardown_module():
    _clean()


def _issue_row(repo_key, num):
    db, db_lock = _db()
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT user_id, assignee, created_by FROM issue WHERE repo_key=? AND issue_number=?",
            (repo_key, num)).fetchone()
    return row


class TestPreserveUserIdOnAssigneeChange:
    """assignee 变更为未注册用户时，已有 user_id 不被清零。"""

    def test_existing_user_id_preserved_when_new_assignee_unmapped(self):
        """已有 user_id=CREATOR_UID 的卡片，assignee 变更为未注册用户后 user_id 不变。"""
        from src.api.issues_center import ensure_issue_task
        db, db_lock = _db()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_by) "
                "VALUES (?, ?, ?, 100, 'handoff card', 'active', ?)",
                (CREATOR_UID, MAPPED, REPO, MAPPED))
            db.commit()
        ensure_issue_task(REPO, 100, title="handoff card", assignee=UNMAPPED, sender=UNMAPPED)
        row = _issue_row(REPO, 100)
        assert row is not None
        # D105：issue.user_id 为 TEXT 口径，按 str 归一比较
        assert str(row[0]) == str(CREATOR_UID), \
            f"user_id should be preserved as {CREATOR_UID}, got {row[0]}"


class TestEnsureIssueTaskWritesCreatedBy:
    """ensure_issue_task 建卡时写入 created_by。"""

    def test_insert_writes_created_by_from_sender(self):
        from src.api.issues_center import ensure_issue_task
        ensure_issue_task(REPO, 101, title="new card", assignee=MAPPED, sender=MAPPED)
        row = _issue_row(REPO, 101)
        assert row is not None
        assert row[2] == MAPPED, f"created_by should be '{MAPPED}', got '{row[2]}'"

    def test_insert_does_not_use_assignee_as_creator(self):
        """#805 创建人口径：assignee ≠ 创建人，仅有 assignee 时不臆造 created_by。"""
        from src.api.issues_center import ensure_issue_task
        ensure_issue_task(REPO, 102, title="no sender", assignee=MAPPED)
        row = _issue_row(REPO, 102)
        assert row is not None
        assert (row[2] or "") == "", \
            f"created_by must stay empty when only assignee is known, got '{row[2]}'"

    def test_reuse_backfills_empty_created_by(self):
        """复用已有行时 created_by 为空且 sender 非空 → 回补。"""
        from src.api.issues_center import ensure_issue_task
        db, db_lock = _db()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_by) "
                "VALUES (?, ?, ?, 103, 'legacy empty cb', 'active', '')",
                (CREATOR_UID, MAPPED, REPO))
            db.commit()
        ensure_issue_task(REPO, 103, title="legacy empty cb", assignee=MAPPED, sender=MAPPED)
        row = _issue_row(REPO, 103)
        assert row is not None
        assert row[2] == MAPPED, f"created_by should be backfilled to '{MAPPED}', got '{row[2]}'"

    def test_reuse_does_not_overwrite_existing_created_by(self):
        """复用时已有 created_by 非空 → 不覆写。"""
        from src.api.issues_center import ensure_issue_task
        db, db_lock = _db()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_by) "
                "VALUES (?, ?, ?, 104, 'existing cb', 'active', 'original_creator')",
                (CREATOR_UID, MAPPED, REPO))
            db.commit()
        ensure_issue_task(REPO, 104, title="existing cb", assignee=MAPPED, sender="someone_else")
        row = _issue_row(REPO, 104)
        assert row is not None
        assert row[2] == "original_creator", f"created_by should not be overwritten, got '{row[2]}'"
