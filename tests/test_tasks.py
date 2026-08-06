"""Tasks 路由测试：notify / close / mine"""
import os
import sys
import asyncio
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import tasks as tasks_mod
from src.session_manager import db, db_lock
from src.user import db as user_db

client = TestClient(app)

_uid = None
_token = ""
_headers = {}


def setup_module():
    global _uid, _token, _headers
    from src.api.db_setup import init_database
    init_database()
    _uid = user_db.create_user("test_tasks_user", "test123")
    if not _uid:
        _uid = user_db.get_user_by_username("test_tasks_user")
    _token = user_db.create_session_token(_uid["id"])
    _headers = {"Authorization": f"Bearer {_token}"}
    # 清理旧映射并重建 gitcode_user -> user_id 映射
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN ('younger_eric','bob')")
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,datetime('now'))",
                  (_uid["id"], "younger_eric", "ou_test_123", "吴扬"))
        db.commit()
    # 建一个托管仓库（供 notify 校验）
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (user_id, repo_owner, repo_name, full_name, webhook_id, is_admin) VALUES (?,?,?,?,?,?)",
                  (_uid["id"], "org", "test", "org/test", 1, 1))
        db.commit()


def teardown_module():
    if _uid:
        user_db.delete_user(_uid["id"])


def _clean_tasks():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM tasks")
        db.commit()


class TestNotify:
    def test_missing_fields(self):
        r = client.post("/api/tasks/notify", json={})
        assert r.status_code == 400
        r = client.post("/api/tasks/notify", json={"assignee": "x", "repo_key": "org/test"})
        assert r.status_code == 400

    def test_repo_not_managed(self):
        r = client.post("/api/tasks/notify", json={
            "assignee": "younger_eric", "repo_key": "nope/missing",
            "issue_number": 1, "task_description": "d"})
        assert r.status_code == 404

    def test_assignee_not_registered(self):
        _clean_tasks()
        r = client.post("/api/tasks/notify", json={
            "assignee": "unknown_user", "repo_key": "org/test",
            "issue_number": 5, "task_description": "处理登录超时"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "未注册" in data["message"]

    def test_normal_path_creates_task_and_conversation(self):
        _clean_tasks()

        def _swallow(coro, *a, **k):
            try:
                coro.close()
            except Exception:
                pass
            return None

        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/notify", json={
                "assignee": "younger_eric", "repo_key": "org/test",
                "issue_number": 5, "task_description": "处理登录超时"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["task_id"] > 0
        assert data["conversation_id"]
        # 任务落库 active
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT user_id, assignee, repo_key, issue_number, status FROM tasks WHERE id=?", (data["task_id"],)).fetchone()
        assert row and row[0] == _uid["id"]
        assert row and row[1] == "younger_eric"
        assert row and row[4] == "active"

    def test_reassign_closes_other_assignee(self):
        _clean_tasks()
        # 先建 alice 任务（未注册则直接插库模拟）
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "alice", "org/test", 7))
            db.commit()
            alice_task = c.lastrowid
        # bob 用独立 user_id 注册（unified_user_mapping.user_id 为主键，避免覆盖 younger_eric）
        bob_uid = user_db.create_user("bob_task_user", "test123")
        if not bob_uid:
            bob_uid = user_db.get_user_by_username("bob_task_user")
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='bob'")
            c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,datetime('now'))",
                      (bob_uid["id"], "bob", "ou_bob", "鲍勃"))
            db.commit()
        def _swallow(coro, *a, **k):
            try:
                coro.close()
            except Exception:
                pass
            return None

        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/notify", json={
                "assignee": "bob", "repo_key": "org/test",
                "issue_number": 7, "task_description": "换人处理"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # alice 任务应被关闭（reassigned）
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM tasks WHERE id=?", (alice_task,)).fetchone()
        assert row and row[0] == "completed"
        assert row and row[1] == "reassigned"

    def test_duplicate_notify_reuses_active(self):
        _clean_tasks()

        def _swallow(coro, *a, **k):
            try:
                coro.close()
            except Exception:
                pass
            return None

        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            client.post("/api/tasks/notify", json={
                "assignee": "younger_eric", "repo_key": "org/test",
                "issue_number": 9, "task_description": "任务A"})
            client.post("/api/tasks/notify", json={
                "assignee": "younger_eric", "repo_key": "org/test",
                "issue_number": 9, "task_description": "任务B"})
        # 同 assignee 同 issue 只应有 1 条 active 任务
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT id, task_description FROM tasks WHERE repo_key='org/test' AND issue_number=9 AND assignee='younger_eric' AND status='active'"
            ).fetchall()
        assert len(rows) == 1, rows


class TestClose:
    def test_close_marks_completed(self):
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "org/test", 11))
            db.commit()
            tid = c.lastrowid
        r = client.post("/api/tasks/close", json={"repo_key": "org/test", "issue_number": 11})
        assert r.status_code == 200
        assert r.json()["closed"] >= 1
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row and row[0] == "completed"
        assert row and row[1] == "issue_closed"

    def test_close_missing_fields(self):
        r = client.post("/api/tasks/close", json={})
        assert r.status_code == 400


class TestMine:
    def test_mine_requires_auth(self):
        r = client.get("/api/tasks/mine")
        assert r.status_code == 401

    def test_mine_returns_only_current_user(self):
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "org/test", 13))
            db.commit()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (9999,?,?,?, 'active')",
                      ("other", "org/test", 14))
            db.commit()
        r = client.get("/api/tasks/mine", headers=_headers)
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        assert all(t["assignee"] in ("younger_eric",) for t in tasks)
        assert all(t["issue_number"] != 14 for t in tasks)

    def test_mine_status_filter(self):
        _clean_tasks()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                      (_uid["id"], "younger_eric", "org/test", 15))
            db.commit()
            c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status, completed_reason, completed_at) VALUES (?,?,?,?, 'completed', 'issue_closed', datetime('now'))",
                      (_uid["id"], "younger_eric", "org/test", 16))
            db.commit()
        r_active = client.get("/api/tasks/mine?status=active", headers=_headers)
        r_done = client.get("/api/tasks/mine?status=completed", headers=_headers)
        assert all(t["status"] == "active" for t in r_active.json()["tasks"])
        assert all(t["status"] == "completed" for t in r_done.json()["tasks"])
