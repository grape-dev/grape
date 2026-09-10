"""Issue #51: 任务生命周期 API 测试——confirm-done / cancel / resume / pipeline"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import tasks as tasks_mod
from src.session_manager import db, db_lock
from src.user import db as user_db

def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)

_uid = None
_token = ""
_headers = {}


def setup_module():
    global _uid, _token, _headers
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username='test_lifecycle_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='lifecycle_tester'")
        db.commit()
    _uid = user_db.create_user("test_lifecycle_user", "test123")
    if not _uid:
        _uid = user_db.get_user_by_username("test_lifecycle_user")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,datetime('now'))",
                  (_uid["id"], "lifecycle_tester", "ou_test_lifecycle", "测试"))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id) VALUES (?,?,?,?,?)",
                  ("gitcode:org/lifecycle", "org", "lifecycle", "org/lifecycle", 1))
        db.commit()


def teardown_module():
    if _uid:
        user_db.delete_user(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username='test_lifecycle_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='lifecycle_tester'")
        c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:org/lifecycle'")
        db.commit()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM tasks")
        c.execute("DELETE FROM issue_state WHERE repo_key='gitcode:org/lifecycle'")
        c.execute("DELETE FROM events WHERE repo_key='gitcode:org/lifecycle'")
        db.commit()


def _insert_task(status='active', issue_number=100, stage=None):
    """插入测试任务，返回 task_id"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO tasks (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (_uid["id"], "lifecycle_tester", "gitcode:org/lifecycle", issue_number,
             "Test Task #" + str(issue_number), "desc", status))
        db.commit()
        tid = c.lastrowid
    if stage:
        from src.api.session_store import set_issue_stage
        set_issue_stage("gitcode:org/lifecycle", f"#{issue_number}", stage, "test")
    return tid


class TestConfirmDone:
    """POST /api/tasks/{task_id}/confirm-done"""

    def test_task_not_found(self):
        _clean()
        r = client.post("/api/tasks/999999/confirm-done", headers=_headers,
                        json={"confirmation": "user_confirmed"})
        assert r.status_code == 404

    def test_already_done_is_idempotent(self):
        _clean()
        tid = _insert_task(status='completed', issue_number=101)
        r = client.post(f"/api/tasks/{tid}/confirm-done", headers=_headers,
                        json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        assert r.json()["already_done"] is True

    def test_confirm_done_from_pending_review(self):
        _clean()
        tid = _insert_task(status='processing', issue_number=102, stage='pending_review')
        r = client.post(f"/api/tasks/{tid}/confirm-done", headers=_headers,
                        json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert r.json()["stage"] == "done"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row[0] == "completed"
        assert row[1] == "user_confirmed"

    def test_confirm_done_rejected_when_not_in_review_stage(self):
        _clean()
        tid = _insert_task(status='active', issue_number=103, stage='vod')
        r = client.post(f"/api/tasks/{tid}/confirm-done", headers=_headers,
                        json={"confirmation": "user_confirmed"})
        assert r.status_code == 400
        assert "尚未到待审核阶段" in r.json()["detail"]["message"]

    def test_confirm_done_from_sre_stage(self):
        """Issue #85: sre 阶段不再允许直接确认，必须先进入 pending_review"""
        _clean()
        tid = _insert_task(status='processing', issue_number=104, stage='sre')
        r = client.post(f"/api/tasks/{tid}/confirm-done", headers=_headers,
                        json={"confirmation": "user_confirmed"})
        assert r.status_code == 400
        assert "尚未到待审核阶段" in r.json()["detail"]["message"]


class TestCancelTask:
    """POST /api/tasks/{task_id}/cancel"""

    def test_task_not_found(self):
        _clean()
        r = client.post("/api/tasks/999999/cancel", headers=_headers)
        assert r.status_code == 404

    def test_already_cancelled_is_idempotent(self):
        _clean()
        tid = _insert_task(status='cancelled', issue_number=110)
        r = client.post(f"/api/tasks/{tid}/cancel", headers=_headers)
        assert r.status_code == 200
        assert r.json()["already_cancelled"] is True

    def test_completed_task_cannot_be_cancelled(self):
        _clean()
        tid = _insert_task(status='completed', issue_number=111)
        r = client.post(f"/api/tasks/{tid}/cancel", headers=_headers)
        assert r.status_code == 400
        assert "不可取消" in r.json()["detail"]["message"]

    def test_cancel_active_task(self):
        _clean()
        tid = _insert_task(status='active', issue_number=112, stage='coding')
        r = client.post(f"/api/tasks/{tid}/cancel", headers=_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row[0] == "cancelled"
        assert row[1] == "user_cancelled"

    def test_cancel_processing_task(self):
        _clean()
        tid = _insert_task(status='processing', issue_number=113, stage='qa')
        r = client.post(f"/api/tasks/{tid}/cancel", headers=_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"


class TestResumeTask:
    """POST /api/tasks/{task_id}/resume"""

    def test_task_not_found(self):
        _clean()
        r = client.post("/api/tasks/999999/resume", headers=_headers)
        assert r.status_code == 404

    def test_resume_non_cancelled_task_rejected(self):
        _clean()
        tid = _insert_task(status='active', issue_number=120)
        r = client.post(f"/api/tasks/{tid}/resume", headers=_headers)
        assert r.status_code == 400
        assert "仅已取消任务可恢复" in r.json()["detail"]["message"]

    def test_resume_cancelled_task(self):
        _clean()
        tid = _insert_task(status='cancelled', issue_number=121, stage='coding')
        r = client.post(f"/api/tasks/{tid}/resume", headers=_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "processing"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row[0] == "processing"
        assert row[1] is None


class TestTaskPipeline:
    """GET /api/tasks/{task_id}/pipeline"""

    def test_task_not_found(self):
        _clean()
        r = client.get("/api/tasks/999999/pipeline", headers=_headers)
        assert r.status_code == 404

    def test_pipeline_returns_nodes_and_events(self):
        _clean()
        tid = _insert_task(status='processing', issue_number=130, stage='coding')
        r = client.get(f"/api/tasks/{tid}/pipeline", headers=_headers)
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data
        assert "events" in data
        assert "current_stage" in data
        assert data["current_stage"] == "coding"

    def test_pipeline_with_done_stage(self):
        _clean()
        tid = _insert_task(status='completed', issue_number=131, stage='done')
        r = client.get(f"/api/tasks/{tid}/pipeline", headers=_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["current_stage"] == "done"
        for n in data["nodes"]:
            assert n["node_status"] == "completed"
