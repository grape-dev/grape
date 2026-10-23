"""Issue #385 — PUT /api/feedback/{id}/status 状态流转接口测试。

覆盖：
- 不存在记录 → 404（detail.code=404，与 DELETE 对齐）
- admin 合法更新状态 → 200 不回归
- 普通用户 → 403（admin-only，weekly-08 feedback-openapi 改造）
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


def _admin_jwt():
    """插入 admin_accounts + 签发 role=admin JWT。"""
    import uuid
    from src.admin.db import get_admin_token_version
    name = f"adm_st_{uuid.uuid4().hex[:6]}"
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO admin_accounts (username, password_hash, is_super, token_version) "
                  "VALUES (?, '', 1, 0)", (name,))
        db.commit()
        aid = c.lastrowid
    return create_access_token(aid, name, get_admin_token_version(aid), "", "admin"), aid


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
        self.admin_token, self.admin_id = _admin_jwt()
        self.admin_hdr = {"Authorization": f"Bearer {self.admin_token}"}

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM user_feedback WHERE user_id=?", (self.uid,))
            _cleanup(*[r[0] for r in c.fetchall()])
            db.execute("DELETE FROM admin_accounts WHERE id=?", (self.admin_id,))
            db.commit()

    def test_update_status_nonexistent_404(self):
        """不存在记录 → 404，detail 结构与 DELETE 一致"""
        r = TestClient(app).put(
            "/api/feedback/999999/status",
            json={"status": "resolved"},
            headers=self.admin_hdr)
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == 404
        assert "不存在" in r.json()["detail"]["message"]

    def test_update_status_valid_record_200(self):
        """合法记录更新状态 → 200，行为不回归"""
        fid = _insert_feedback(self.uid, self.username)
        r = TestClient(app).put(
            f"/api/feedback/{fid}/status",
            json={"status": "resolved", "handler_name": "admin"},
            headers=self.admin_hdr)
        assert r.status_code == 200
        assert r.json()["success"] is True
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, handler_name FROM user_feedback WHERE id=?", (fid,))
            row = c.fetchone()
            assert row[0] == "resolved"
            assert row[1]  # handler_name 来自 admin token username（不信任 body）
        _cleanup(fid)

    def test_update_status_requires_admin(self):
        """普通用户 JWT → 403（admin-only 语义变更，weekly-08）"""
        fid = _insert_feedback(self.uid, self.username)
        r = TestClient(app).put(
            f"/api/feedback/{fid}/status",
            json={"status": "resolved"},
            headers=self.hdr)
        assert r.status_code == 403
        _cleanup(fid)

    def test_update_status_invalid_value_unchanged(self):
        """无效状态值分支行为不变（200 + error 字典，Out of Scope）"""
        fid = _insert_feedback(self.uid, self.username)
        r = TestClient(app).put(
            f"/api/feedback/{fid}/status",
            json={"status": "bogus"},
            headers=self.admin_hdr)
        assert r.status_code == 200
        assert "error" in r.json()
        _cleanup(fid)

    def test_update_deleted_record_404(self):
        """软删除记录 → 404"""
        fid = _insert_feedback(self.uid, self.username)
        with db_lock:
            db.execute("UPDATE user_feedback SET is_deleted=1 WHERE id=?", (fid,))
            db.commit()
        r = TestClient(app).put(
            f"/api/feedback/{fid}/status",
            json={"status": "closed"},
            headers=self.admin_hdr)
        assert r.status_code == 404
