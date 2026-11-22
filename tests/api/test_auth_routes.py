"""认证路由测试（src/domains/mgmt/routes/auth.py）：OAuth 发起 / 回调全流程 / 错误分支。

mock urllib 网络调用与 meta 配置，不触真实 gitcode.com；state 走真实 DB 校验。
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from src.api.main import app
from src.domains.mgmt.user import db as user_db
from src.kernel.session_manager import db, db_lock
# JWT access token helper（替代旧的 create_session_token）
def _jwt_access_token(user_id, username="test"):
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)


class _FakeReqResp:
    """模拟 requests.get/post 的返回值。"""

    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self):
        pass


def _assert_whitelist_empty():
    """断言白名单表为空（不重复发无 WHERE 全表 DELETE）。

    清表由 TestOAuthCallback.setup_method 统一负责；测试库已由
    conftest.py 隔离到 data/test_sessions.db（绝不触碰生产 data/sessions.db），
    且 pytest 串行执行（addopts -p no:randomly，未启用 xdist），故此处只做状态断言。
    """
    with db_lock:
        cnt = db.cursor().execute("SELECT COUNT(*) FROM login_whitelist").fetchone()[0]
    assert cnt == 0, f"login_whitelist 应为空（setup_method 未生效）：{cnt} 行"


def _cleanup(username: str, gitcode_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (username,))
        db.commit()
    # D32: 清理 grape_accounts + external_identities
    from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username, soft_delete_account, delete_external_identity
    acc = get_grape_account_by_username(username)
    if acc:
        delete_external_identity(acc["id"], "gitcode")
        soft_delete_account(acc["id"])


def _save_state():
    state = "test-state-" + uuid.uuid4().hex[:8]
    user_db.save_oauth_state(state, "gitcode", {})
    return state


def _gid():
    return str(10_000_000 + uuid.uuid4().int % 100_000_000)


class TestOAuthLogin:
    def test_missing_client_id_400(self):
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value=""):
            r = client.get("/api/auth/login/gitcode")
        assert r.status_code == 400
        assert "不支持" in r.json()["detail"]["message"] or "未配置" in r.json()["detail"]["message"]

    def test_success_builds_authorize_url(self, monkeypatch):
        # 需求 11：redirect_uri 从 provider 配置读取
        # 真实读链：mgmt/auth/providers.get_redirect_uri → get_provider（本模块
        # :124）——runtime.vcs.providers 包无 get_provider（vcs providers 是方言
        # 翻译模块），历史 mock 挂错模块 AttributeError（基线红）。
        import src.domains.mgmt.auth.providers as _prov
        _orig_get_provider = _prov.get_provider
        def _mock_get_provider(name):
            cfg = _orig_get_provider(name)
            if cfg:
                cfg = dict(cfg)
                cfg["redirect_uri"] = "https://grape.test/cb"
            return cfg
        monkeypatch.setattr(_prov, "get_provider", _mock_get_provider)
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="cli_test"):
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
        # 本机 configs/server-config.json 配了对外基址（OAuth/安装命令要用），
        # 会让「按 Host 兜底」这条路失效；测兜底逻辑必须屏蔽机器级配置。
        monkeypatch.setattr("src.kernel.config.read_server_config", lambda: {})
        # 只返回 client_id（其余 meta 为空 → 走 host 构造 redirect_uri）
        def fake(key):
            if key == "oauth_gitcode_client_id":
                return "cli_x"
            return ""

        with patch("src.domains.mgmt.user.db.get_meta_value", side_effect=fake):
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
                c.execute("DELETE FROM login_whitelist WHERE id > 0")  # F5：ratchet 禁无 WHERE 整表 DELETE
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
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", side_effect=OSError("net down")):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_no_access_token_redirect(self):
        state = _save_state()
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", return_value=_FakeReqResp({"refresh_token": "rt"})):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_user_info_exception_redirect(self):
        state = _save_state()
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", return_value=_FakeReqResp({"access_token": "at"})), \
             patch("requests.get", side_effect=OSError("user api down")):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_whitelist_denies_new_user_redirect(self):
        state = _save_state()
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": 777777, "login": "whitelist_denied_user",
                                              "nickname": "拒绝"})), \
             patch("src.kernel.config.is_user_allowed", return_value=False):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers.get("location", "")
        assert "WHITELIST" in loc
        # P0-1（Issue #3054）：跳转 URL 只放错误码，不夹带第三方 external_login
        assert "whitelist_denied_user" not in loc, loc
        # P0-3（Issue #3054）：只做一次编码 —— ':' 编码为 %3A，不能再套一层（%253A）
        assert "WHITELIST%3Agitcode" in loc, loc
        assert "%253A" not in loc, loc

    def test_oauth_error_redirect_single_encoded_and_no_raw_percent(self):
        """P0-3（Issue #3054）：_oauth_error_redirect 只经 urlencode 一次编码。

        双重编码会让前端被迫二次 decode，遇裸 % 值就抛 URIError 中断 init。
        """
        from starlette.requests import Request
        from src.domains.mgmt.routes.grape_auth import _oauth_error_redirect
        scope = {
            "type": "http", "method": "GET", "path": "/api/auth/callback/gitcode",
            "headers": [(b"host", b"grape.test")], "query_string": b"",
            "scheme": "http", "server": ("grape.test", 80), "client": ("127.0.0.1", 1),
        }
        req = Request(scope)
        with patch("src.domains.mgmt.user.db.get_browser_callback_url", return_value=""):
            resp = _oauth_error_redirect(req, "100%")
            resp2 = _oauth_error_redirect(req, "WHITELIST:gitcode")
        loc = resp.headers["location"]
        assert "oauth_error=100%25" in loc, loc      # 单次编码
        assert "100%2525" not in loc, loc            # 不是二次编码
        loc2 = resp2.headers["location"]
        assert "oauth_error=WHITELIST%3Agitcode" in loc2, loc2
        assert "%253A" not in loc2, loc2

    def test_success_new_user_auto_create(self):
        """D35: 未绑定用户 → 自动创建账号 + 直接登录（不再跳关联页）。"""
        uname = "oauth_new_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state()
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "nickname": "新用户"})), \
             patch("src.kernel.config.is_user_allowed", return_value=True), \
             patch("src.domains.mgmt.auth.signup_policy.check_oauth_signup_allowed", return_value=True):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        # P0-3: 回调现在用 exchange_code 替代 URL token
        assert "exchange_code=" in r.headers["location"]
        _cleanup(uname, gid)

    def test_auto_create_blocked_when_signup_closed(self):
        """P3-6 主路径：注册关闭时 OAuth 自动建号必须被拒（原实现只挡了二选一页两分支）。

        默认 allow_signup 未配置 = 关闭（signup_policy 的 fail-closed 口径），
        因此 OAuth 首次登录不得静默建号；应带 signup_closed 错误码回跳。
        """
        uname = "oauth_closed_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state()
        # get_meta_value 返回 "x"：渠道已配置（可进 callback），而 allow_signup 非
        # true/1/yes → 注册关闭（signup_policy 的默认关闭口径）。
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "nickname": "新用户"})), \
             patch("src.kernel.config.is_user_allowed", return_value=True):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "signup_closed" in r.headers["location"]
        # 不得留下账号
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username
        assert get_grape_account_by_username(uname) is None
        _cleanup(uname, gid)

    def test_provider_mismatch_does_not_burn_state(self):
        """P3-5 主路径：错平台回调不得消费合法 state（先 peek 再 DELETE）。

        原实现 callback 走 validate_oauth_state(state) 默认 consume=True，
        provider 比对在其后 → 一次串味/误点就烧掉合法 state（线上 state_invalid）。
        """
        from src.domains.mgmt.user import db as user_db

        state = "test-state-" + uuid.uuid4().hex[:8]
        user_db.save_oauth_state(state, "gitcode", {})
        # 用 github 的 URL 打 gitcode 的 state
        r = client.get("/api/auth/callback/github", params={"state": state, "code": "c"},
                       follow_redirects=False)
        assert r.status_code == 302
        assert "provider_mismatch" in r.headers["location"]
        # 关键：state 仍在（未被烧掉），可被正确平台继续使用
        peek = user_db.validate_oauth_state(state, "gitcode", consume=False)
        assert peek is not None, "provider mismatch 把合法 state 消费掉了"

    def test_success_existing_user_keeps_repo_selection(self):
        """已绑定用户 → 直接登录，first_login=0。"""
        from src.domains.mgmt.auth.grape_account_db import create_grape_account, create_external_identity
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
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
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


    def test_bound_user_bypasses_whitelist_when_enabled(self):
        """口径锁（2026-11-12 拍板）：白名单是准入闸，已绑定用户登录不查白名单。

        实查依据：live 库验收账号（gitee external_login=gitee-login） 在白名单
        清空+开关开启（fail-closed）后仍 login_success —— grape_auth.py:1590 命中
        binding 即直接登录，1719 的 _check_whitelist 到不了。此处用真实
        is_user_allowed（不 mock），且表为空+开关开启，确保走的就是 fail-closed 状态。
        """
        from src.domains.mgmt.auth.grape_account_db import create_grape_account, create_external_identity
        import src.kernel.config as cfg
        uname = "oauth_bound_" + uuid.uuid4().hex[:6]
        gid = _gid()
        u = create_grape_account(username=uname, email="", password="pw")
        create_external_identity(u["id"], "gitcode", gid, uname, "已绑定")
        _assert_whitelist_empty()               # 表空（由 setup_method 已清理，此处只断言不重删）
        cfg._WL_ENABLED_CACHE = True            # 开关开（钉住缓存，避免 meta mock 干扰）
        cfg._WL_CACHE_TS = float("inf")
        state = _save_state()
        try:
            with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
                 patch("requests.post",
                       return_value=_FakeReqResp({"access_token": "at3", "refresh_token": "rt"})), \
                 patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "name": "已绑定"})):
                r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                               follow_redirects=False)
            assert r.status_code == 302
            assert "WHITELIST" not in r.headers.get("location", ""), r.headers.get("location")
            assert "exchange_code=" in r.headers["location"]
        finally:
            cfg.invalidate_login_whitelist_cache()
            _cleanup(uname, gid)

    def test_unbound_user_denied_when_whitelist_empty_and_enabled(self):
        """对照组（fail-closed 端到端）：同一状态下未绑定用户必须被白名单拒绝。"""
        import src.kernel.config as cfg
        uname = "oauth_unbound_" + uuid.uuid4().hex[:6]
        gid = _gid()
        _assert_whitelist_empty()               # 表空（由 setup_method 已清理，此处只断言不重删）
        cfg._WL_ENABLED_CACHE = True
        cfg._WL_CACHE_TS = float("inf")
        state = _save_state()
        try:
            with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
                 patch("requests.post",
                       return_value=_FakeReqResp({"access_token": "at4", "refresh_token": "rt"})), \
                 patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "nickname": "新"})):
                r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                               follow_redirects=False)
            assert r.status_code == 302
            assert "WHITELIST" in r.headers.get("location", "")
        finally:
            cfg.invalidate_login_whitelist_cache()
            _cleanup(uname, gid)


    def test_no_gitcode_id_redirect(self):
        state = _save_state()
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
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
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "nickname": "x"})), \
             patch("src.kernel.config.is_user_allowed", return_value=True), \
             patch("src.domains.mgmt.routes.grape_auth.create_grape_account_oauth",
                   return_value=None):

            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                           follow_redirects=False)
        # grape_auth callback 建号失败（create_grape_account_oauth=None；D32 后 user_accounts
        # 死表写函数已删除，建号唯一路径是 grape_accounts）
        # 仍走 302 重定向（oauth_error），不 500
        assert r.status_code == 302

    def test_mapping_write_failure_still_logs_in(self):
        """set_feishu_open_id 失败不应阻断登录（仅告警）。"""
        uname = "oauth_map_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state()
        with patch("src.domains.mgmt.user.db.get_meta_value", return_value="x"), \
             patch("requests.post",
                   return_value=_FakeReqResp({"access_token": "at"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "nickname": "x"})), \
             patch("src.kernel.config.is_user_allowed", return_value=True), \
             patch("src.kernel.session_manager.set_feishu_open_id", side_effect=RuntimeError("boom")):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                           follow_redirects=False)
        assert r.status_code == 302
        _cleanup(uname, gid)


class TestProfileLogout:
    def _auth_headers(self):
        from src.domains.mgmt.auth.grape_account_db import create_grape_account
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
            from src.domains.mgmt.auth.grape_account_db import soft_delete_account
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
            from src.domains.mgmt.auth.grape_account_db import soft_delete_account
            soft_delete_account(u["id"])
