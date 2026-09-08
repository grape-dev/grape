"""任务中心全覆盖（task-center-issue-coverage-design.md §3-A）回归测试。

覆盖：ensure_issue_task 幂等 / assignee 解析 / 未注册落库隐藏 / owner 兜底 / close 兜底。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest.mock import patch

from src.api.routes import tasks as tasks_mod
from src.session_manager import db, db_lock

REPO = "gitcode:owner/repo"


def _count(repo_key: str = REPO, issue: int = 1):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM tasks WHERE repo_key=? AND issue_number=?", (repo_key, issue))
        return c.fetchone()[0]


def _rows(repo_key: str = REPO, issue: int = 1):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, assignee, status, title, task_description FROM tasks WHERE repo_key=? AND issue_number=?",
                  (repo_key, issue))
        return c.fetchall()


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM tasks WHERE repo_key=?", (REPO,))
        db.commit()


def setup_function():
    _cleanup()


def teardown_function():
    _cleanup()


def test_ensure_idempotent_single_row():
    """同一 issue 重复建档只产生一条 active 行（幂等）"""
    with patch.object(tasks_mod, "_get_user_id_by_gitcode", return_value=42), \
         patch.object(tasks_mod, "_get_issue_title", return_value="T"):
        t1 = tasks_mod.ensure_issue_task(REPO, 1, title="T", assignee="alice", event_type="issue_opened")
        t2 = tasks_mod.ensure_issue_task(REPO, 1, title="T", assignee="alice", event_type="issue_comment")
    assert t1 == t2
    assert _count(REPO, 1) == 1
    rows = _rows(REPO, 1)
    assert rows[0][0] == 42 and rows[0][1] == "alice" and rows[0][2] == "active"


def test_ensure_assignee_unmapped_hidden_user0():
    """assignee 未注册平台 → user_id=0 落库（任务中心 user_id=? 查询自然不可见）"""
    with patch.object(tasks_mod, "_get_user_id_by_gitcode", return_value=None), \
         patch.object(tasks_mod, "_get_issue_title", return_value="T"):
        tasks_mod.ensure_issue_task(REPO, 2, title="T", assignee="ghost", event_type="issue_opened")
    rows = _rows(REPO, 2)
    assert rows and rows[0][0] == 0 and rows[0][1] == "ghost"


def test_ensure_owner_fallback_when_no_assignee():
    """无 assignee → 仓库 owner；owner 映射到 user_id"""
    with patch.object(tasks_mod, "_get_user_id_by_gitcode", side_effect=lambda u: 7 if u == "owner" else None), \
         patch.object(tasks_mod, "_get_issue_title", return_value=""):
        tasks_mod.ensure_issue_task(REPO, 3, assignee="", event_type="issue_opened")
    rows = _rows(REPO, 3)
    assert rows and rows[0][0] == 7 and rows[0][1] == "owner"


def test_close_issue_tasks_completes_active():
    """close_issue_tasks：active/processing → completed(issue_closed)"""
    with patch.object(tasks_mod, "_get_user_id_by_gitcode", return_value=1), \
         patch.object(tasks_mod, "_get_issue_title", return_value="T"):
        tasks_mod.ensure_issue_task(REPO, 4, title="T", assignee="bob", event_type="issue_opened")
        tasks_mod.ensure_issue_task(REPO, 5, title="T", assignee="bob", event_type="issue_opened")
    n = tasks_mod.close_issue_tasks(REPO, 4)
    assert n == 1
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, completed_reason FROM tasks WHERE repo_key=? AND issue_number=4", (REPO,))
        row = c.fetchone()
    assert row and row[0] == "completed" and row[1] == "issue_closed"
    # issue 5 未关闭 → 仍 active
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM tasks WHERE repo_key=? AND issue_number=5", (REPO,))
        row = c.fetchone()
    assert row and row[0] == "active"


def test_ensure_owner_fallback_unmapped_user0():
    """无 assignee 且 owner 未映射 → user_id=0 落库（target 仍为 owner login）"""
    with patch.object(tasks_mod, "_get_user_id_by_gitcode", return_value=None), \
         patch.object(tasks_mod, "_get_issue_title", return_value="T"):
        tasks_mod.ensure_issue_task(REPO, 6, title="T", assignee="", event_type="issue_opened")
    rows = _rows(REPO, 6)
    assert rows and rows[0][0] == 0 and rows[0][1] == "owner"


def test_my_tasks_shows_unowned_rows():
    """mine 展示 user_id=0 的任务（P2 修订：展示不隐藏）"""
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.api.deps import get_current_user, get_current_user_or_api_key

    with patch.object(tasks_mod, "_get_user_id_by_gitcode", return_value=None), \
         patch.object(tasks_mod, "_get_issue_title", return_value="Unowned"):
        tasks_mod.ensure_issue_task(REPO, 7, title="Unowned", assignee="ghost", event_type="issue_opened")

    app.dependency_overrides[get_current_user] = lambda: {"id": 999, "username": "viewer"}
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {"id": 999, "username": "viewer"}
    try:
        client = TestClient(app)
        r = client.get("/api/tasks/mine")
        assert r.status_code == 200
        titles = [t["title"] for t in r.json().get("tasks", [])]
        assert "Unowned" in titles
    finally:
        app.dependency_overrides.clear()


def test_ensure_links_conversation():
    """建档任务自动关联「Issue #N」对话（任务中心「去对话」配套）"""
    with patch.object(tasks_mod, "_get_user_id_by_gitcode", return_value=88), \
         patch.object(tasks_mod, "_get_issue_title", return_value="Linked"):
        tid = tasks_mod.ensure_issue_task(REPO, 8, title="Linked", assignee="carol", event_type="issue_opened")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT conversation_id FROM tasks WHERE id=?", (tid,))
        conv_id = c.fetchone()[0]
    assert conv_id, "任务应关联对话"
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, conv_type, issue_number FROM chat_conversations WHERE id=?", (conv_id,))
        row = c.fetchone()
    assert row and row[0] == conv_id and row[1] == "task" and row[2] == 8
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM chat_messages WHERE conversation_id=?", (conv_id,))
        assert c.fetchone()[0] >= 1, "新对话应写入任务上下文消息"


def test_ensure_skips_conversation_when_user0():
    """user_id=0（assignee/owner 均未注册）→ 不建对话，conversation_id 留空"""
    with patch.object(tasks_mod, "_get_user_id_by_gitcode", return_value=None), \
         patch.object(tasks_mod, "_get_issue_title", return_value="NoUser"):
        tid = tasks_mod.ensure_issue_task(REPO, 9, title="NoUser", assignee="ghost2", event_type="issue_opened")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT conversation_id FROM tasks WHERE id=?", (tid,))
        conv_id = c.fetchone()[0]
    assert not conv_id