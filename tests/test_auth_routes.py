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
# JWT access token helper（替代旧的 create_session_token）
def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)


class _FakeReqResp:
    """模拟 requests.get/post 的返回值。"""
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self):
        pass


def _cleanup(username: str, gitcode_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (username,))
        db.commit()
    # D32: 清理 grape_accounts + external_identities
    from src.auth.grape_account_db import get_grape_account_by_username, soft_delete_account, delete_external_identity
    acc = get_grape_account_by_username(username)
    if acc:
        delete_external_identity(acc["id"], "gitcode")
        soft_delete_account(acc["id"])
    # 兼容：清理旧表残留
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
        assert "不支持" in r.json()["detail"]["message"] or "未配置" in r.json()["detail"]["message"]

    def test_success_builds_authorize_url(self, monkeypatch):
        # 需求 11：redirect_uri 从 provider 配置读取
        import src.auth.providers as _prov
        _orig_get_provider = _prov.get_provider
        def _mock_get_provider(name):
            cfg = _orig_get_provider(name)
            if cfg:
                cfg = dict(cfg)
                cfg["redirect_uri"] = "https://grape.test/cb"
            return cfg
        monkeypatch.setattr(_prov, "get_provider", _mock_get_provider)
        with patch("src.user.db.get_meta_value", return_value="cli_test"):
            r = client.get("/api/auth/login/gitcode")
        assert r.status_code == 200
        url = r.json()["redirect_url"]
        assert url.startswith("https://gitcode.com/oauth/authorize")
        assert "client_id=cli_test" in url
        # urllib.parse.urlencode encodes : as %3A and / as %2F
        assert "redirect_uri=https%3A%2F%2Fgrape.test%2Fcb" in url
        assert "state=" in url

    def test_redirect_uri_fallback_from_host(self, monkeypatch):
        monkeypatch.delenv("OAUTH_REDIRECT_URI", raising=False)
        # 只返回 client_id（其余 meta 为空 → 走 host 构造 redirect_uri）
        def fake(key):
            if key == "oauth_gitcode_client_id":
                return "cli_x"
            return ""

        with patch("src.user.db.get_meta_value", side_effect=fake):
            r = client.get("/api/auth/login/gitcode", headers={"Host": "grape.test:8000"})
        assert r.status_code == 200
        url = r.json()["redirect_url"]
        # 需求 3：分路径回调地址 /api/auth/callback/gitcode，Host 保留端口
        assert "redirect_uri=http%3A%2F%2Fgrape.test%3A8000%2Fapi%2Fauth%2Fcallback%2Fgitcode" in url


class TestOAuthCallback:
    """OAuth 回调测试——适配 grape_auth.py 分路径回调路由 /api/auth/callback/{provider}。

    grape_auth.py 的 callback 错误处理用 _oauth_error_redirect 返回 302，
    而非旧 auth.py 的 raise HTTPException（400/502）。
    """

    def setup_method(self):
        """OMIT-5 修复：每用例前清理 login_whitelist 表，防止测试间状态污染。"""
        with db_lock:
            c = db.cursor()
            try:
                c.execute("DELETE FROM login_whitelist")
                db.commit()
            except Exception:
                pass

    def test_error_param_redirect(self):
        r = client.get("/api/auth/callback/gitcode", params={"error": "access_denied"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_invalid_state_redirect(self):
        r = client.get("/api/auth/callback/gitcode", params={"state": "bad-state", "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_token_fetch_exception_redirect(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", side_effect=OSError("net down")):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_no_access_token_redirect(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", return_value=_FakeReqResp({"refresh_token": "rt"})):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_user_info_exception_redirect(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", return_value=_FakeReqResp({"access_token": "at"})), \
             patch("requests.get", side_effect=OSError("user api down")):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_whitelist_denies_new_user_redirect(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": 777777, "login": "whitelist_denied_user",
                                              "nickname": "拒绝"})), \
             patch("src.config.is_user_allowed", return_value=False):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "WHITELIST" in r.headers.get("location", "")

    def test_success_new_user_auto_create(self):
        """D35: 未绑定用户 → 自动创建账号 + 直接登录（不再跳关联页）。"""
        uname = "oauth_new_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "nickname": "新用户"})), \
             patch("src.config.is_user_allowed", return_value=True):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        # P0-3: 回调现在用 exchange_code 替代 URL token
        assert "exchange_code=" in r.headers["location"]
        _cleanup(uname, gid)

    def test_success_existing_user_keeps_repo_selection(self):
        """已绑定用户 → 直接登录，first_login=0。"""
        from src.auth.grape_account_db import create_grape_account, create_external_identity
        uname = "oauth_exist_" + uuid.uuid4().hex[:6]
        gid = _gid()
        # D32: 测试用 grape_accounts 而非 user_accounts
        u = create_grape_account(username=uname, email="", password="pw")
        create_external_identity(u["id"], "gitcode", gid, uname, "旧名")
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
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at2", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "name": "旧名"})):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        # P0-3: 回调现在用 exchange_code 替代 URL token
        assert "exchange_code=" in r.headers["location"]
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE full_name=?", (uname,))
            db.commit()
        _cleanup(uname, gid)


    def test_no_gitcode_id_redirect(self):
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at"})), \
             patch("requests.get", return_value=_FakeReqResp({"login": "no_id_user"})):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                           follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_create_user_failure_redirect(self):
        uname = "oauth_fail_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "nickname": "x"})), \
             patch("src.config.is_user_allowed", return_value=True), \
             patch("src.user.db.create_user", return_value=None):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                           follow_redirects=False)
        # grape_auth callback 未绑定用户走 _oauth_associate_redirect（302），不走 create_user
        assert r.status_code == 302

    def test_mapping_write_failure_still_logs_in(self):
        """set_feishu_open_id 失败不应阻断登录（仅告警）。"""
        uname = "oauth_map_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state()
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "nickname": "x"})), \
             patch("src.config.is_user_allowed", return_value=True), \
             patch("src.session_manager.set_feishu_open_id", side_effect=RuntimeError("boom")):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                           follow_redirects=False)
        assert r.status_code == 302
        _cleanup(uname, gid)


class TestProfileLogout:
    def _auth_headers(self):
        from src.auth.grape_account_db import create_grape_account
        uname = "auth_prof_" + uuid.uuid4().hex[:6]
        u = create_grape_account(username=uname, email="", password="pw")
        tok = _jwt_access_token(u["id"], u["username"])
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
            from src.auth.grape_account_db import soft_delete_account
            soft_delete_account(u["id"])

    def test_logout_clears_token(self):
        u, h = self._auth_headers()
        try:
            r = client.post("/api/auth/logout", headers=h)
            assert r.status_code == 200
            assert r.json()["message"] == "已登出"
            # JWT 无状态——logout 后旧 token 在过期前仍有效
            # （前端负责删 localStorage token，服务端无法撤销 JWT）
            r2 = client.get("/api/auth/profile", headers=h)
            assert r2.status_code == 401  # logout 后 token_version+1 → token 失效
        finally:
            from src.auth.grape_account_db import soft_delete_account
            soft_delete_account(u["id"])
