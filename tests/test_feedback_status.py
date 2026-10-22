"""Issue #385 — PUT /api/feedback/{id}/status 状态流转接口测试。

覆盖：
- 不存在记录 → 404（detail.code=404，与 DELETE 对齐）
- 合法记录更新状态 → 200 不回归
- 无效状态值 → 行为不变（200 + error 字典，Out of Scope）
"""
import sys, os

# 强制使用独立测试库
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock

from src.api.jwt_utils import create_access_token
from src.auth.grape_account_db import get_token_version


def _jwt(user_id, username):
    return create_access_token(user_id, username, get_token_version(user_id))


def _insert_feedback(user_id, username, title="状态测试反馈"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO user_feedback "
            "(user_id, username, category, title, description, status) "
            "VALUES (?,?,?,?,?, 'open')",
            (user_id, username, "bug", title, "测试描述"))
        db.commit()
        return c.lastrowid


def _cleanup(*fids):
    with db_lock:
        for fid in fids:
            db.execute("DELETE FROM user_feedback WHERE id=?", (fid,))
        db.commit()


class TestFeedbackUpdateStatus:
    def setup_method(self):
        self.uid = 990385
        self.username = "fb_status_385"
        self.token = _jwt(self.uid, self.username)
        self.hdr = {"Authorization": f"Bearer {self.token}"}

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM user_feedback WHERE user_id=?", (self.uid,))
            _cleanup(*[r[0] for r in c.fetchall()])

    def test_update_status_nonexistent_404(self):
        """不存在记录 → 404，detail 结构与 DELETE 一致"""
        r = TestClient(app).put(
            "/api/feedback/999999/status",
            json={"status": "resolved"},
            headers=self.hdr)
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == 404
        assert "不存在" in r.json()["detail"]["message"]

    def test_update_status_valid_record_200(self):
        """合法记录更新状态 → 200，行为不回归"""
        fid = _insert_feedback(self.uid, self.username)
        r = TestClient(app).put(
            f"/api/feedback/{fid}/status",
            json={"status": "resolved", "handler_name": "admin"},
            headers=self.hdr)
        assert r.status_code == 200
        assert r.json()["success"] is True
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, handler_name FROM user_feedback WHERE id=?", (fid,))
            row = c.fetchone()
            assert row[0] == "resolved"
            assert row[1] == "admin"

    def test_update_status_invalid_value_unchanged(self):
        """无效状态值分支行为不变（200 + error 字典，Out of Scope）"""
        fid = _insert_feedback(self.uid, self.username)
        r = TestClient(app).put(
            f"/api/feedback/{fid}/status",
            json={"status": "bogus"},
            headers=self.hdr)
        assert r.status_code == 200
        assert "error" in r.json()

    def test_update_deleted_record_404(self):
        """软删除记录 → 404"""
        fid = _insert_feedback(self.uid, self.username)
        with db_lock:
            db.execute("UPDATE user_feedback SET is_deleted=1 WHERE id=?", (fid,))
            db.commit()
        r = TestClient(app).put(
            f"/api/feedback/{fid}/status",
            json={"status": "closed"},
            headers=self.hdr)
        assert r.status_code == 404
