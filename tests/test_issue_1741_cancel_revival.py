"""Issue #1741：待澄清需求取消后不得复活 / 评论取消生效 / 恢复不产生重复卡片。

覆盖：
- 取消 → 注入迟到事件（ensure_issue_task）→ 仍为 cancelled；
- 评论 /cancel 路径对 awaiting_input 卡片生效；
- 恢复后同一 issue 仍只有一张卡片（不产生重复）；
- 同一 repo_key+issue_number 历史多行时取消/恢复全行幂等。
"""
import asyncio
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api import issues_center as ic
from src.api.db_setup import init_database
from src.session_manager import db, db_lock

init_database()

REPO = "gitcode:org/issue1741"


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,))
        db.commit()


def _seed(issue_number: int, status: str, user_id: int = 0, assignee: str = "alice") -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, assignee, REPO, issue_number, "t", "d", status))
        _id = c.lastrowid
        db.commit()
        return _id


def _row(issue_number: int, issue_id: int | None = None):
    with db_lock:
        c = db.cursor()
        if issue_id is not None:
            c.execute("SELECT id, status, completed_reason FROM issue WHERE id=?", (issue_id,))
        else:
            c.execute(
                "SELECT id, status, completed_reason FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
                (REPO, issue_number))
        return c.fetchone()


def _count(issue_number: int) -> int:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM issue WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        return c.fetchone()[0]


def _seed_agent_task(issue_number: int, status: str = "running") -> str:
    import uuid

    _id = f"at-{uuid.uuid4().hex[:10]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, prompt, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_id, "sess", 1, None, f"#{issue_number}", REPO, "", status))
        db.commit()
    return _id


def _agent_status(task_id: str) -> str:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        return row[0] if row else ""


def _drop_unique_index():
    with db_lock:
        c = db.cursor()
        c.execute("DROP INDEX IF EXISTS idx_tasks_repo_issue")
        db.commit()


def _restore_unique_index():
    with db_lock:
        c = db.cursor()
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_repo_issue ON issue(repo_key, issue_number)")
        db.commit()


def setup_function():
    _cleanup()


def teardown_function():
    _cleanup()
    _restore_unique_index()


def test_cancelled_not_revived_by_late_event():
    """取消后注入迟到事件（ensure_issue_task）不得复活为 active/awaiting_input。"""
    _seed(1, "awaiting_input")
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=42), \
         patch.object(ic, "_get_issue_title", return_value="late"):
        n = ic.cancel_issue_task(REPO, 1)
        assert n == 1
        row = _row(1)
        assert row[1] == "cancelled" and row[2] == "user_cancelled"

        for _ in range(2):
            ic.ensure_issue_task(REPO, 1, title="late event", assignee="alice",
                                 event_type="issue_comment")
            row = _row(1)
            assert row[1] == "cancelled", f"late event revived cancelled card: {row}"


def test_comment_cancel_works_for_awaiting_input():
    """评论 /cancel 路径（cancel_issue_task）对 awaiting_input 卡片生效。"""
    _seed(2, "awaiting_input")
    n = ic.cancel_issue_task(REPO, 2)
    assert n == 1
    row = _row(2)
    assert row[1] == "cancelled" and row[2] == "user_cancelled"


def test_comment_cancel_also_covers_pending_review():
    """评论 /cancel 路径同样覆盖 pending_review（补齐状态集合）。"""
    _seed(3, "pending_review")
    assert ic.cancel_issue_task(REPO, 3) == 1
    assert _row(3)[1] == "cancelled"


def test_resume_after_cancel_no_duplicate_card():
    """取消后恢复：回到 active 且同一 issue 仍只有一张卡片。"""
    _seed(4, "awaiting_input")
    assert ic.cancel_issue_task(REPO, 4) == 1
    assert ic.resume_issue_task(REPO, 4) == 1
    row = _row(4)
    assert row[1] == "active" and row[2] is None
    assert _count(4) == 1
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=42), \
         patch.object(ic, "_get_issue_title", return_value="resume"):
        ic.ensure_issue_task(REPO, 4, title="resume", assignee="alice", event_type="issue_comment")
    assert _count(4) == 1, "resume+late event created duplicate card"


def test_route_cancel_and_resume_all_rows_idempotent():
    """历史多行：route cancel/resume 对同一 repo_key+issue_number 全行幂等。"""
    from src.api.routes.issues import cancel_issue, resume_issue

    _drop_unique_index()
    _seed(5, "awaiting_input")
    _seed(5, "processing")
    _seed(5, "active")
    first_id = _row(5)[0]
    try:
        assert asyncio.run(cancel_issue(first_id, user={"id": 999}))["status"] == "cancelled"
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=?", (REPO, 5))
            statuses = [r[0] for r in c.fetchall()]
        assert statuses == ["cancelled"] * 3, statuses

        with patch("urllib.request.urlopen"):
            assert asyncio.run(resume_issue(first_id, user={"id": 999}))["status"] == "processing"
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=?", (REPO, 5))
            statuses = [r[0] for r in c.fetchall()]
        assert statuses == ["processing"] * 3, statuses
        assert _count(5) == 3, "resume must not insert duplicate cards"
    finally:
        _cleanup()
        _restore_unique_index()


def test_repeat_cancel_is_idempotent_no_completed_at_refresh():
    """重复取消已是 cancelled 的行不得刷新 completed_at，同时仍收口残留可取消行。"""
    from src.api.routes.issues import cancel_issue

    _drop_unique_index()
    main_id = _seed(6, "awaiting_input")
    _seed(6, "active")
    try:
        assert asyncio.run(cancel_issue(main_id, user={"id": 999}))["status"] == "cancelled"
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE issue SET completed_at='2000-01-01 00:00:00' WHERE id=?", (main_id,))
            db.commit()

        result = asyncio.run(cancel_issue(main_id, user={"id": 999}))
        assert result.get("already_cancelled") is True
        with db_lock:
            c = db.cursor()
            c.execute("SELECT completed_at FROM issue WHERE id=?", (main_id,))
            assert c.fetchone()[0] == "2000-01-01 00:00:00", "already-cancelled row refreshed completed_at"
            c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id", (REPO, 6))
            assert [r[0] for r in c.fetchall()] == ["cancelled", "cancelled"]
    finally:
        _cleanup()
        _restore_unique_index()


def test_already_cancelled_main_closes_duplicate_running_agents():
    """主卡已 cancelled 的幂等分支也要终止历史重复行上残留的 running agent_tasks。"""
    from src.api.routes.issues import cancel_issue

    _drop_unique_index()
    main_id = _seed(7, "cancelled")
    dup_id = _seed(7, "awaiting_input")
    task_id = _seed_agent_task(7, status="running")
    try:
        result = asyncio.run(cancel_issue(main_id, user={"id": 999}))
        assert result.get("already_cancelled") is True
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM issue WHERE id=?", (dup_id,))
            assert c.fetchone()[0] == "cancelled"
        assert _agent_status(task_id) == "cancelled"
    finally:
        _cleanup()
        _restore_unique_index()
