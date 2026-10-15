"""Issue #177 — 反馈软删除接口测试。

覆盖：
- 本人删除自己的反馈 → 200 + is_deleted=1
- 非本人删除 → 403
- 删除已删除的记录 → 404
- GET /mine 过滤已删除记录
- GET /list 过滤已删除记录
"""
import sys, os

# 强制使用独立测试库
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.user import db as user_db
from src.auth import grape_account_db
from src.session_manager import db, db_lock

from src.api.jwt_utils import create_access_token
from src.auth.grape_account_db import get_token_version


def _jwt(user_id, username):
    return create_access_token(user_id, username, get_token_version(user_id))


def _ensure_user(username, password="test123"):
    uid = grape_account_db.create_grape_account(username, email=None, password=password)
    if not uid:
        uid = grape_account_db.get_grape_account_by_username(username)
    return uid


def _insert_feedback(user_id, username, title="测试反馈"):
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


class TestFeedbackDelete:
    def setup_method(self):
        self.user_a = _ensure_user("fb_del_a")
        self.user_b = _ensure_user("fb_del_b")
        self.token_a = _jwt(self.user_a["id"], self.user_a["username"])
        self.token_b = _jwt(self.user_b["id"], self.user_b["username"])
        self.hdr_a = {"Authorization": f"Bearer {self.token_a}"}
        self.hdr_b = {"Authorization": f"Bearer {self.token_b}"}

    def teardown_method(self):
        fids = []
        with db_lock:
            c = db.cursor()
            for uid in (self.user_a["id"], self.user_b["id"]):
                c.execute("SELECT id FROM user_feedback WHERE user_id=?", (uid,))
                fids.extend([r[0] for r in c.fetchall()])
        _cleanup(*fids)

    def test_delete_own_feedback(self):
        """本人删除自己的反馈 → 200 + is_deleted=1"""
        fid = _insert_feedback(self.user_a["id"], self.user_a["username"])
        r = TestClient(app).delete(f"/api/feedback/{fid}", headers=self.hdr_a)
        assert r.status_code == 200
        assert r.json()["success"] is True
        # 验证软删除标记
        with db_lock:
            c = db.cursor()
            c.execute("SELECT is_deleted FROM user_feedback WHERE id=?", (fid,))
            assert c.fetchone()[0] == 1

    def test_delete_other_user_feedback_403(self):
        """非本人删除 → 403"""
        fid = _insert_feedback(self.user_a["id"], self.user_a["username"])
        r = TestClient(app).delete(f"/api/feedback/{fid}", headers=self.hdr_b)
        assert r.status_code == 403
        assert "无权" in r.json()["detail"]["message"]

    def test_delete_already_deleted_404(self):
        """删除已删除的记录 → 404"""
        fid = _insert_feedback(self.user_a["id"], self.user_a["username"])
        client = TestClient(app)
        # 先删除
        r1 = client.delete(f"/api/feedback/{fid}", headers=self.hdr_a)
        assert r1.status_code == 200
        # 再删除 → 404
        r2 = client.delete(f"/api/feedback/{fid}", headers=self.hdr_a)
        assert r2.status_code == 404

    def test_delete_nonexistent_404(self):
        """删除不存在的记录 → 404"""
        r = TestClient(app).delete("/api/feedback/999999", headers=self.hdr_a)
        assert r.status_code == 404

    def test_mine_filters_deleted(self):
        """GET /mine 过滤已删除记录"""
        fid1 = _insert_feedback(self.user_a["id"], self.user_a["username"], "保留的")
        fid2 = _insert_feedback(self.user_a["id"], self.user_a["username"], "要删的")
        client = TestClient(app)
        # 删除 fid2
        r = client.delete(f"/api/feedback/{fid2}", headers=self.hdr_a)
        assert r.status_code == 200
        # 查 mine 列表
        r = client.get("/api/feedback/mine", headers=self.hdr_a)
        assert r.status_code == 200
        items = r.json()["items"]
        ids = [it["id"] for it in items]
        assert fid1 in ids
        assert fid2 not in ids

    def test_list_filters_deleted(self):
        """GET /list 过滤已删除记录"""
        fid = _insert_feedback(self.user_a["id"], self.user_a["username"], "待删除列表测试")
        client = TestClient(app)
        # 删除
        r = client.delete(f"/api/feedback/{fid}", headers=self.hdr_a)
        assert r.status_code == 200
        # 查 admin 列表
        r = client.get("/api/feedback/list")
        assert r.status_code == 200
        ids = [it["id"] for it in r.json()["items"]]
        assert fid not in ids

    def test_stats_filters_deleted(self):
        """GET /stats 过滤已删除记录"""
        fid = _insert_feedback(self.user_a["id"], self.user_a["username"], "统计测试")
        client = TestClient(app)
        # 删除前统计
        r1 = client.get("/api/feedback/stats")
        before = r1.json()["byStatus"]["open"]
        # 删除
        r = client.delete(f"/api/feedback/{fid}", headers=self.hdr_a)
        assert r.status_code == 200
        # 删除后统计
        r2 = client.get("/api/feedback/stats")
        after = r2.json()["byStatus"]["open"]
        assert after == before - 1

    def test_delete_no_auth_401(self):
        """无认证删除 → 401"""
        fid = _insert_feedback(self.user_a["id"], self.user_a["username"])
        r = TestClient(app).delete(f"/api/feedback/{fid}")
        assert r.status_code == 401