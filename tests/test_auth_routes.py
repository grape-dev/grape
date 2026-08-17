"""认证路由测试（src/api/routes/auth.py）：OAuth 发起 / 回调全流程 / 错误分支。

mock urllib 网络调用与 meta 配置，不触真实 gitcode.com；state 走真实 DB 校验。
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from src.api.main import app
from src.user import db as user_db
from src.session_manager import db, db_lock

client = TestClient(app)


class _FakeUrlResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def _cleanup(username: str, gitcode_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (username,))
        db.commit()
    u = user_db.get_user_by_username(username)
    if u:
        user_db.delete_user(u["id"])


def _save_state():
    state = "test-state-" + uuid.uuid4().hex[:8]
    user_db.save_oauth_state(state, "gitcode", {})
    return state


def _gid():
    return str(10_000_000 + uuid.uuid4().int % 100_000_000)


class TestOAuthLogin:
    def test_missing_client_id_400(self):
        with patch("src.user.db.get_meta_value", return_value=""):
            r = client.get("/api/auth/login/gitcode")
        assert r.status_code == 400
        assert "未配置" in r.json()["detail"]["message"]

    def test_success_builds_authorize_url(self, monkeypatch):
        monkeypatch.setenv("OAUTH_REDIRECT_URI", "https://grape.test/cb")
        with patch("src.user.db.get_meta_value", return_value="cli_test"):
            r = client.get("/api/auth/login/gitcode")
        assert r.status_code == 200
        url = r.json()["redirect_url"]
        assert url.startswith("https://gitcode.com/oauth/authorize")
        assert "client_id=cli_test" in url
        assert "redirect_uri=https%3A//grape.test/cb" in url
        assert "state=" in url

    def test_redirect_uri_fallback_from_host(self, monkeypatch):
        monkeypatch.delenv("OAUTH_REDIRECT_URI", raising=False)
        # 只返回一次 client_id（其余 meta 为空 → 走 host 构造 redirect_uri）
        real = user_db.get_meta_value

        def fake(key):
            if key == "oauth_gitcode_client_id":
                return "cli_x"
            return ""

        with patch("src.user.db.get_meta_value", side_effect=fake):
            r = client.get("/api/auth/login/gitcode", headers={"Host": "grape.test:8000"})
        assert r.status_code == 200
        url = r.json()["redirect_url"]
        assert "redirect_uri=http%3A//grape.test%3A80/api/auth/callback/gitcode" in url


class TestOAuthCallback:
    def test_error_param_400(self):
        r = client.get("/api/auth/callback/gitcode", params={"error": "access_denied"})
        assert r.status_code == 400
        assert "access_denied" in r.json()["detail"]["message"]

    def test_invalid_state_400(self):
        r = client.get("/api/auth/callback/gitcode", params={"state": "bad-state", "code": "c"})
        assert r.status_code == 400
        assert "state" in r.json()["detail"]["message"]

    def test_token_fetch_exception_502(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("urllib.request.urlopen", side_effect=OSError("net down")):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 502

    def test_no_access_token_502(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("urllib.request.urlopen", return_value=_FakeUrlResp({"refresh_token": "rt"})):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 502

    def test_user_info_exception_502(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("urllib.request.urlopen", side_effect=[_FakeUrlResp({"access_token": "at"}),
                                                          OSError("user api down")]):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 502

    def test_whitelist_denies_new_user_403(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("urllib.request.urlopen",
                   side_effect=[_FakeUrlResp({"access_token": "at", "refresh_token": "rt"}),
                                _FakeUrlResp({"id": 777777, "login": "whitelist_denied_user",
                                              "nickname": "拒绝"})]), \
             patch("src.config.is_gitcode_user_allowed", return_value=False):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 403
        assert "白名单" in r.json()["detail"]["message"]

    def test_success_creates_new_user_and_redirects(self):
        uname = "oauth_new_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("urllib.request.urlopen",
                   side_effect=[_FakeUrlResp({"access_token": "at", "refresh_token": "rt"}),
                                _FakeUrlResp({"id": gid, "login": uname, "nickname": "新用户"})]), \
             patch("src.config.is_gitcode_user_allowed", return_value=True):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "token=" in r.headers["location"]
        assert "first_login=1" in r.headers["location"]  # 新用户无托管仓库
        # 落库校验
        u = user_db.get_user_by_gitcode_id(gid)
        assert u and u["username"] == uname
        acc = user_db.get_user_account(uname) or {}
        assert acc.get("gitcode_api_token") == "at"
        _cleanup(uname, gid)

    def test_success_existing_user_keeps_repo_selection(self):
        uname = "oauth_exist_" + uuid.uuid4().hex[:6]
        gid = _gid()
        # 预建用户并绑定 gitcode_id + 托管仓库（first_login 应为 0）
        u = user_db.create_user(uname, "pw")
        user_db.link_gitcode_user(u["id"], gid, "old_at")
        with db_lock:
            c = db.cursor()
            rk = "gitcode:%s" % uname
            c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
                      "VALUES (?, 'o', 'r', ?, 1, 'b')", (rk, uname))
            c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
                      "VALUES (?, ?, 'o', 'r', 1, 1)", (u["id"], rk))
            db.commit()
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("urllib.request.urlopen",
                   side_effect=[_FakeUrlResp({"access_token": "at2", "refresh_token": "rt"}),
                                _FakeUrlResp({"id": gid, "login": uname, "name": "旧名"})]):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "first_login=0" in r.headers["location"]
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE full_name=?", (uname,))
            db.commit()
        _cleanup(uname, gid)


    def test_no_gitcode_id_502(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("urllib.request.urlopen",
                   side_effect=[_FakeUrlResp({"access_token": "at"}),
                                _FakeUrlResp({"login": "no_id_user"})]):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                           follow_redirects=False)
        assert r.status_code == 502
        assert "用户信息" in r.json()["detail"]["message"]

    def test_create_user_failure_500(self):
        uname = "oauth_fail_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("urllib.request.urlopen",
                   side_effect=[_FakeUrlResp({"access_token": "at"}),
                                _FakeUrlResp({"id": gid, "login": uname, "nickname": "x"})]), \
             patch("src.config.is_gitcode_user_allowed", return_value=True), \
             patch("src.user.db.create_user", return_value=None):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                           follow_redirects=False)
        assert r.status_code == 500

    def test_mapping_write_failure_still_logs_in(self):
        """set_feishu_open_id 失败不应阻断登录（仅告警）。"""
        uname = "oauth_map_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("urllib.request.urlopen",
                   side_effect=[_FakeUrlResp({"access_token": "at"}),
                                _FakeUrlResp({"id": gid, "login": uname, "nickname": "x"})]), \
             patch("src.config.is_gitcode_user_allowed", return_value=True), \
             patch("src.session_manager.set_feishu_open_id", side_effect=RuntimeError("boom")):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                           follow_redirects=False)
        assert r.status_code == 302
        _cleanup(uname, gid)


class TestProfileLogout:
    def _auth_headers(self):
        u = user_db.create_user("auth_prof_" + uuid.uuid4().hex[:6], "pw")
        tok = user_db.create_session_token(u["id"])
        return u, {"Authorization": f"Bearer {tok}"}

    def test_profile_returns_user(self):
        u, h = self._auth_headers()
        try:
            r = client.get("/api/auth/profile", headers=h)
            assert r.status_code == 200
            data = r.json()
            assert data["username"] == u["username"]
            assert "managed_repos" in data and "gitcode_bound" in data
        finally:
            user_db.delete_user(u["id"])

    def test_logout_clears_token(self):
        u, h = self._auth_headers()
        try:
            r = client.post("/api/auth/logout", headers=h)
            assert r.status_code == 200
            assert r.json()["message"] == "已登出"
            # token 已失效
            r2 = client.get("/api/auth/profile", headers=h)
            assert r2.status_code == 401
        finally:
            user_db.delete_user(u["id"])
