""""issue.user_id 归属正确性" 回归测试（issues-mine-user-id-zero-fix-design.md）。

覆盖：
1. session_store._sync_issue_fields(ensure_row=True) 最小建档：assignee 可映射 → user_id=映射 id；不可映射 → 0。
2. ensure_issue_task 幂等复用已有 user_id=0 行时回补 user_id（assignee 已可映射）。
3. scripts.backfill_issue_user_id.candidates() 能列出待回填行。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock  # noqa: E402

REPO = "gitcode:owner/uid-attribution-test"
MAPPED = "mapped_creator"
UNMAPPED = "unmapped_user_xyz"


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN (?, ?)", (MAPPED, UNMAPPED))
        db.commit()


def setup_function():
    _cleanup()
    with db_lock:
        c = db.cursor()
        # MAPPED 用户 → user_id 771（稳定）；UNMAPPED 不建映射
        c.execute(
            "INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, gitcode_nickname, updated_at) "
            "VALUES (771, ?, 'Mapped Creator', datetime('now'))", (MAPPED,))
        db.commit()


def teardown_function():
    _cleanup()


def _issue_user_id(repo_key, num):
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT user_id FROM issue WHERE repo_key=? AND issue_number=?",
                        (repo_key, num)).fetchone()
    return row[0] if row else None


class TestEnsureRowMinimalCard:
    def test_mapped_assignee_gets_real_user_id(self):
        """ensure_row 最小建档：assignee 可映射 → user_id=771 而非 0。"""
        from src.api.session_store import _sync_issue_fields
        _sync_issue_fields(REPO, "#51", {"stage": "vod"}, ensure_row=True, assignee=MAPPED)
        assert _issue_user_id(REPO, 51) == 771

    def test_unmapped_assignee_keeps_zero(self):
        """ensure_row 最小建档：assignee 不可映射 → user_id 保持 0（合法兜底，不凭空归属）。"""
        from src.api.session_store import _sync_issue_fields
        _sync_issue_fields(REPO, "#52", {"stage": "vod"}, ensure_row=True, assignee=UNMAPPED)
        assert _issue_user_id(REPO, 52) == 0


class TestEnsureIssueTaskBackfill:
    def test_reuse_backfills_user_id_when_zero_and_mappable(self):
        """先建 user_id=0 的存量行，ensure_issue_task 复用时应回补为 771。"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
                "VALUES (0, ?, ?, 53, 'legacy zero row', 'active')", (MAPPED, REPO))
            db.commit()
        from src.api.routes.tasks import ensure_issue_task
        tid = ensure_issue_task(REPO, 53, title="legacy zero row", assignee=MAPPED)
        assert tid is not None
        assert _issue_user_id(REPO, 53) == 771

    def test_reuse_does_not_touch_correct_user_id(self):
        """user_id 已正确（=771）时不改动。"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
                "VALUES (771, ?, ?, 54, 'ok row', 'active')", (MAPPED, REPO))
            db.commit()
        from src.api.routes.tasks import ensure_issue_task
        ensure_issue_task(REPO, 54, title="ok row", assignee=MAPPED)
        assert _issue_user_id(REPO, 54) == 771


class TestBackfillScript:
    def test_script_candidates_lists_zero_rows(self):
        from scripts.backfill_issue_user_id import candidates
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
                "VALUES (0, ?, ?, 61, 'backfill me', 'active')", (MAPPED, REPO))
            db.commit()
        rows = candidates()
        hit = [r for r in rows if r["repo_key"] == REPO and r["issue_number"] == 61]
        assert len(hit) == 1 and hit[0]["mapped_user_id"] == 771
        # 不可映射的不在候选内
        c = db.cursor()
        c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
                  "VALUES (0, ?, ?, 62, 'no mapping', 'active')", (UNMAPPED, REPO))
        db.commit()
        rows2 = candidates()
        assert not [r for r in rows2 if r["repo_key"] == REPO and r["issue_number"] == 62]