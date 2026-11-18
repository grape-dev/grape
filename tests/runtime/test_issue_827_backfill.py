"""Issue #827: 回填脚本 created_by 回退源测试。

覆盖：assignee 不可映射但 created_by 可映射的行出现在候选列表。
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

REPO = "gitcode:backfill827/demo"
MAPPED = "mapped_backfill_827"
UNMAPPED = "unmapped_backfill_827"


def _db():
    from src.kernel.session_manager import db, db_lock
    return db, db_lock


def _clean():
    db, db_lock = _db()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN (?, ?)", (MAPPED, UNMAPPED))
        db.commit()


def setup_module():
    from src.api import db_setup
    try:
        db_setup.init_database()
    except Exception:
        pass
    _clean()
    db, db_lock = _db()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO unified_user_mapping (user_id, gitcode_user, updated_at) "
            "VALUES (8271, ?, datetime('now'))", (MAPPED,))
        db.commit()


def teardown_module():
    _clean()


class TestBackfillCreatedByFallback:
    """assignee 不可映射但 created_by 可映射 → 列入候选。"""

    def test_candidate_lists_row_with_mappable_created_by(self):
        from scripts.backfill_issue_user_id import candidates
        db, db_lock = _db()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_by) "
                "VALUES (0, ?, ?, 300, 'cb backfill me', 'active', ?)",
                (UNMAPPED, REPO, MAPPED))
            db.commit()
        rows = candidates()
        hit = [r for r in rows if r["repo_key"] == REPO and r["issue_number"] == 300]
        assert len(hit) == 1, f"should find 1 candidate via created_by, got {len(hit)}"
        assert hit[0]["mapped_user_id"] == 8271

    def test_unmappable_both_skipped(self):
        from scripts.backfill_issue_user_id import candidates
        db, db_lock = _db()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_by) "
                "VALUES (0, ?, ?, 301, 'no mapping at all', 'active', ?)",
                (UNMAPPED, REPO, UNMAPPED))
            db.commit()
        rows = candidates()
        hit = [r for r in rows if r["repo_key"] == REPO and r["issue_number"] == 301]
        assert len(hit) == 0, f"unmappable both should be skipped, got {len(hit)}"


class TestBackfillApplyCreatedByFallback:
    """apply() 回填 created_by 可映射的行。"""

    def test_apply_backfills_created_by_source(self):
        from scripts.backfill_issue_user_id import candidates, apply
        db, db_lock = _db()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_by) "
                "VALUES (0, ?, ?, 302, 'apply cb', 'active', ?)",
                (UNMAPPED, REPO, MAPPED))
            db.commit()
        n = apply()
        assert n >= 1
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT user_id FROM issue WHERE repo_key=? AND issue_number=?",
                (REPO, 302)).fetchone()
        # D105：issue.user_id 为 TEXT 口径，按 str 归一比较
        assert row and str(row[0]) == "8271", \
            f"user_id should be backfilled to 8271, got {row[0] if row else 'None'}"
