"""Issue #384 — 社区点赞/评论/获取对不存在帖子返回 404 测试。

覆盖：
- GET /api/community/posts/{pid} 不存在 → 404
- POST /api/community/posts/{pid}/like 不存在 → 404
- POST /api/community/posts/{pid}/comments 不存在 → 404
- 合法帖子点赞/评论 → 200 不回归
"""
import sys, os

# 强制使用独立测试库
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock

from src.api.jwt_utils import create_access_token
from src.auth.grape_account_db import get_token_version, create_grape_account, get_grape_account_by_username


def _jwt(user_id, username):
    return create_access_token(user_id, username, get_token_version(user_id))


def _ensure_user(username, password="test123"):
    uid = create_grape_account(username, email=None, password=password)
    if not uid:
        uid = get_grape_account_by_username(username)
    return uid


def _insert_post(user_id, username, title="404 测试帖"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO community_posts (user_id, username, title, content, category) "
            "VALUES (?,?,?,?, 'tech')",
            (user_id, username, title, "测试内容"))
        db.commit()
        return c.lastrowid


def _cleanup(*pids):
    with db_lock:
        for pid in pids:
            db.execute("DELETE FROM community_posts WHERE id=?", (pid,))
            db.execute("DELETE FROM community_comments WHERE post_id=?", (pid,))
            db.execute("DELETE FROM community_post_likes WHERE post_id=?", (pid,))
        db.commit()


class TestCommunityNotFound404:
    def setup_method(self):
        self.user = _ensure_user("comm_404_384")
        self.uid = self.user["id"]
        self.username = self.user["username"]
        self.token = _jwt(self.uid, self.username)
        self.hdr = {"Authorization": f"Bearer {self.token}"}
        self.client = TestClient(app)

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM community_posts WHERE user_id=?", (self.uid,))
            _cleanup(*[r[0] for r in c.fetchall()])

    def test_get_post_nonexistent_404(self):
        r = self.client.get("/api/community/posts/999999")
        assert r.status_code == 404

    def test_like_nonexistent_404(self):
        r = self.client.post("/api/community/posts/999999/like", headers=self.hdr)
        assert r.status_code == 404

    def test_comment_nonexistent_404(self):
        r = self.client.post(
            "/api/community/posts/999999/comments",
            json={"content": "hello"},
            headers=self.hdr)
        assert r.status_code == 404

    def test_like_valid_post_200(self):
        pid = _insert_post(self.uid, self.username)
        r = self.client.post(f"/api/community/posts/{pid}/like", headers=self.hdr)
        assert r.status_code == 200
        assert r.json()["liked"] is True
        # 再点赞一次 → 取消
        r2 = self.client.post(f"/api/community/posts/{pid}/like", headers=self.hdr)
        assert r2.status_code == 200
        assert r2.json()["liked"] is False

    def test_comment_valid_post_200(self):
        pid = _insert_post(self.uid, self.username)
        r = self.client.post(
            f"/api/community/posts/{pid}/comments",
            json={"content": "nice post"},
            headers=self.hdr)
        assert r.status_code == 200
        assert r.json()["message"] == "ok"
