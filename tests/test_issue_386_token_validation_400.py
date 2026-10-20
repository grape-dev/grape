"""Issue #386: 第三方 token 校验失败应返回 400 而非 401。

背景：PUT /api/settings/credentials 传无效 gitcode_token/github_token 时，
后端原先 raise HTTPException(401, "令牌无效或已过期")——与调用方 JWT 鉴权 401
同构，前端全局 401 拦截器误判登录失效踢出会话。

本测试验证（requests mock，不发真实网络请求）：
  1. GitHub 上游 401            → 本服务 400（message 含 "GitHub"）
  2. GitHub 上游 500（其他非200）→ 本服务 400
  3. GitCode 上游 401           → 本服务 400（message 含 "GitCode"）
  4. GitCode 上游 403（其他非200）→ 本服务 400
  5. GitHub 上游网络异常        → 502 维持
  6. GitCode 上游网络异常       → 502 维持
  7. 回归：空字符串清除 gitcode_token 仍走删除绑定（不触发校验）

运行：python -m pytest tests/test_issue_386_token_validation_400.py -v
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# 强制使用独立测试库，防止 TestClient 误连生产库（与 test_api_routes.py 同款双保险）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock

client = TestClient(app)

_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    from src.auth.password import hash_password
    from src.api.jwt_utils import create_access_token
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, token_version FROM grape_accounts WHERE username='test_386'")
        row = c.fetchone()
        if row:
            _uid = {"id": row[0]}
            ver = row[1] or 0
        else:
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                ("test_386", None, hash_password("test123"), "test_386"))
            db.commit()
            _uid = {"id": c.lastrowid}
            ver = 0
        # 清理本用户可能残留的 external_identities，保证用例间状态干净
        c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (_uid["id"],))
        db.commit()
    _headers = {"Authorization": "Bearer " + create_access_token(_uid["id"], "test_386", ver)}


def teardown_module():
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE id=?", (_uid["id"],))
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (_uid["id"],))
            db.commit()


def _mock_resp(status_code=200, json_data=None):
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = json_data or {}
    return m


class TestGitHubTokenValidation400:
    """GitHub 分支：上游 401 / 其他非 200 → 400；网络异常 → 502"""

    def test_upstream_401_returns_400(self):
        with patch("requests.get", return_value=_mock_resp(401)):
            r = client.put("/api/settings/credentials",
                           json={"github_token": "ghp_invalid_token_xxx"},
                           headers=_headers)
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["code"] == 400
        assert "GitHub" in detail["message"]
        assert "令牌无效" not in detail["message"]  # 不再复用调用方鉴权 401 文案

    def test_upstream_500_returns_400(self):
        with patch("requests.get", return_value=_mock_resp(500)):
            r = client.put("/api/settings/credentials",
                           json={"github_token": "ghp_invalid_token_yyy"},
                           headers=_headers)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == 400

    def test_network_error_returns_502(self):
        import requests as _requests
        with patch("requests.get", side_effect=_requests.ConnectionError("boom")):
            r = client.put("/api/settings/credentials",
                           json={"github_token": "ghp_invalid_token_zzz"},
                           headers=_headers)
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["code"] == 502


class TestGitCodeTokenValidation400:
    """GitCode 分支：上游 401 / 其他非 200 → 400；网络异常 → 502"""

    def test_upstream_401_returns_400(self):
        with patch("requests.get", return_value=_mock_resp(401)):
            r = client.put("/api/settings/credentials",
                           json={"gitcode_token": "demo"},
                           headers=_headers)
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["code"] == 400
        assert "GitCode" in detail["message"]

    def test_upstream_403_returns_400(self):
        with patch("requests.get", return_value=_mock_resp(403)):
            r = client.put("/api/settings/credentials",
                           json={"gitcode_token": "some_bad_token"},
                           headers=_headers)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == 400

    def test_network_error_returns_502(self):
        import requests as _requests
        with patch("requests.get", side_effect=_requests.ConnectionError("boom")):
            r = client.put("/api/settings/credentials",
                           json={"gitcode_token": "some_bad_token"},
                           headers=_headers)
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["code"] == 502


class TestClearPathRegression:
    """回归：空字符串清除仍走删除绑定，不触发上游校验"""

    def test_empty_gitcode_token_clears_binding_without_validation(self):
        # 预置一条 gitcode 绑定
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO external_identities "
                "(grape_user_id, provider, external_id, external_login, access_token) "
                "VALUES (?, 'gitcode', 'gc_386', 'test_386', 'tok_386')",
                (_uid["id"],))
            db.commit()
        # requests.get 若被调用则报错（走真实网络）——清除路径不应触发校验
        with patch("requests.get", side_effect=AssertionError("clear path must not call upstream")):
            r = client.put("/api/settings/credentials",
                           json={"gitcode_token": ""},
                           headers=_headers)
        assert r.status_code == 200, r.text
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT access_token FROM external_identities "
                "WHERE grape_user_id=? AND provider='gitcode'",
                (_uid["id"],))
            row = c.fetchone()
        assert not row or row[0] == ""


if __name__ == "__main__":
    unittest.main(verbosity=2)
