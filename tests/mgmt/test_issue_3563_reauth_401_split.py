"""Issue #3563 B 侧：三入口 401 reauth_required 分流用例（Spec v2）。

三入口（check-name / 模板一键创建 / 托管权限判断）在令牌失效且刷新不可用时
统一 401 + detail={action=reauth_required, provider, message}（不新增 error_code，
Issue 评论 202381001/202380151 裁定）；响应不含内部 API URL。

附带：
- check-name 上游 5xx 脱敏短消息（502 语义保留，仅剥 URL）；
- repo_manager get_user_{gitcode,github}_login_status (login, status) 变体；
- oauth_token 统一出口 helper 契约（含 raise_reauth_required / sanitize）。
"""

import os
import uuid

import pytest

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import require_no_session_token, require_org_member
from src.kernel.session_manager import db, db_lock

client = TestClient(app)


def _mk_github_oauth_user(expires_in: int = 60):
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, create_grape_account)
    uname = "s3563b_" + uuid.uuid4().hex[:6]
    u = create_grape_account(username=uname, email=f"{uname}@test.local", password="pw123")
    uid = str(u["id"])
    ok = create_external_identity(
        grape_user_id=uid, provider="github",
        external_id="gh3563b_" + uuid.uuid4().hex[:6],
        external_login=uname, access_token="expired_tok_3563", refresh_token="rt_3563",
        expires_in=expires_in)
    assert ok
    return {"id": uid, "username": uname, "login_provider": "github"}


def _cleanup(user: dict):
    uid = user["id"]
    with db_lock:
        db.execute("DELETE FROM external_identities WHERE grape_user_id=? AND provider=?", (uid, "github"))
        db.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        db.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (user["username"],))
        db.commit()


@pytest.fixture()
def patched_refresh_failure(monkeypatch):
    """刷新不可用（预刷新与被动刷新都失败）+ 调用记录。"""
    calls = []

    def _fake(user_id, provider):
        calls.append((user_id, provider))
        return None

    monkeypatch.setattr("src.api.deps._try_refresh_user_token", _fake)
    monkeypatch.setattr("src.domains.mgmt.user.db.refresh_oauth_token",
                        lambda user_id, provider: None)
    return calls


# ─────────────────────────── 入口 1：check-name ───────────────────────────

class TestCheckRepoNameReauth:
    def setup_method(self):
        self.user = _mk_github_oauth_user()
        app.dependency_overrides[require_no_session_token] = lambda: self.user

    def teardown_method(self):
        app.dependency_overrides.pop(require_no_session_token, None)
        _cleanup(self.user)

    def _patch_verify_401(self, monkeypatch):
        from src.domains.mgmt.vcs import GitHubProvider

        def _boom(self_, token):
            raise RuntimeError("GitHub API GET /user failed (401): Bad credentials")

        monkeypatch.setattr(GitHubProvider, "verify_token", _boom)

    def test_expired_token_returns_401_reauth_required(self, monkeypatch, patched_refresh_failure):
        """过期令牌 + 刷新不可用 → 401 reauth_required（provider=github），不再 502 零信息文案。"""
        self._patch_verify_401(monkeypatch)
        r = client.get("/api/repos/check-name", params={"name": "some-repo"})
        assert r.status_code == 401, r.text
        detail = r.json()["detail"]
        assert detail.get("action") == "reauth_required"
        assert detail.get("provider") == "github"
        assert detail.get("message") == "GitHub 授权已过期，请重新登录授权"
        assert "error_code" not in detail
        assert "api.github.com" not in r.text, "响应不得含内部 API URL"
        # 失败链顺带验证：mark_identity_expired 落 status=4
        from src.domains.mgmt.auth.grape_account_db import is_identity_expired
        assert is_identity_expired(self.user["id"], "github") is True

    def test_valid_token_repo_exists_200(self, monkeypatch, patched_refresh_failure):
        """正常令牌回归：200 exists=true（现路径经 with_oauth_token，行为不变）。"""
        from src.domains.mgmt.vcs import GitHubProvider
        monkeypatch.setattr(GitHubProvider, "verify_token",
                            lambda self_, token: {"login": "gh_user_3563"})
        from src.domains.runtime.vcs.api_client import VcsApiClient
        monkeypatch.setattr(VcsApiClient, "get_repo_info",
                            lambda self_: {"id": 1, "full_name": "gh_user_3563/some-repo"})
        r = client.get("/api/repos/check-name", params={"name": "some-repo"})
        assert r.status_code == 200, r.text
        assert r.json() == {"exists": True, "full_name": "gh_user_3563/some-repo"}

    def test_upstream_5xx_stays_502_sanitized(self, monkeypatch, patched_refresh_failure):
        """非 401 上游错误保留 502 语义，message 脱敏（剥内部 URL）。"""
        from src.domains.mgmt.vcs import GitHubProvider
        monkeypatch.setattr(GitHubProvider, "verify_token",
                            lambda self_, token: {"login": "gh_user_3563"})
        from src.domains.runtime.vcs.api_client import VcsApiClient
        monkeypatch.setattr(
            VcsApiClient, "get_repo_info",
            lambda self_: {"error": "GitHub API GET https://api.github.com/repos/gh_user_3563/some-repo failed (500): server boom"})
        r = client.get("/api/repos/check-name", params={"name": "some-repo"})
        assert r.status_code == 502, r.text
        msg = r.json()["detail"]["message"]
        assert "检查失败" not in msg, "Issue #4231：前缀由前端统一加，后端只回裸原因"
        assert "api.github.com" not in r.text, "脱敏：内部 URL 不得透出"
        assert "(500)" in msg

    def test_repo_404_means_not_exists(self, monkeypatch, patched_refresh_failure):
        """404 → exists=false（存在性语义回归）。"""
        from src.domains.mgmt.vcs import GitHubProvider
        monkeypatch.setattr(GitHubProvider, "verify_token",
                            lambda self_, token: {"login": "gh_user_3563"})
        from src.domains.runtime.vcs.api_client import VcsApiClient
        monkeypatch.setattr(VcsApiClient, "get_repo_info", lambda self_: {"error": "HTTP 404"})
        r = client.get("/api/repos/check-name", params={"name": "some-repo"})
        assert r.status_code == 200
        assert r.json() == {"exists": False}


# ─────────────────────────── 入口 2：模板一键创建 ───────────────────────────

class TestCreateFromTemplateReauth:
    def setup_method(self):
        self.user = _mk_github_oauth_user()
        app.dependency_overrides[require_org_member] = lambda: {
            **self.user, "org_id": "", "org_role": "owner"}

    def teardown_method(self):
        app.dependency_overrides.pop(require_org_member, None)
        _cleanup(self.user)

    def _fake_template(self, monkeypatch):
        from src.domains.mgmt.routes import templates as templates_mod
        monkeypatch.setattr(templates_mod, "_find_template", lambda tid: {
            "template": {"id": "tpl-3563", "source": "local", "scene_id": "",
                         "default_repo_name": "repo-3563", "description": "d"},
            "scene_id": ""})

    def test_expired_token_returns_401_reauth_required(self, monkeypatch, patched_refresh_failure):
        """过期令牌 + 刷新不可用 → 401 reauth_required（此前 get_user_login 吞 401 → 502）。"""
        self._fake_template(monkeypatch)
        from src.domains.mgmt.vcs import GitHubProvider

        def _boom(self_, token):
            raise RuntimeError("GitHub API GET /user failed (401): Bad credentials")

        monkeypatch.setattr(GitHubProvider, "verify_token", _boom)
        r = client.post("/api/project-templates/create",
                        json={"template_id": "tpl-3563", "repo_name": "repo-3563",
                              "provider": "github"})
        assert r.status_code == 401, r.text
        detail = r.json()["detail"]
        assert detail.get("action") == "reauth_required"
        assert detail.get("provider") == "github"
        assert detail.get("message") == "GitHub 授权已过期，请重新登录授权"
        assert "error_code" not in detail
        assert "api.github.com" not in r.text

    def test_create_success_uses_refreshed_token(self, monkeypatch, patched_refresh_failure):
        """成功路径回归：身份经 verify_token、建仓用实际生效 token，200。"""
        self._fake_template(monkeypatch)
        from src.domains.mgmt.vcs import GitHubProvider
        seen_tokens = []
        monkeypatch.setattr(GitHubProvider, "verify_token",
                            lambda self_, token: {"login": "gh_user_3563"} if not seen_tokens.append(token) else {})
        monkeypatch.setattr(GitHubProvider, "create_repo",
                            lambda self_, token, name, description="", **kwargs:
                            {"full_name": f"gh_user_3563/{name}"} if not seen_tokens.append(token) else {})
        from src.domains.mgmt.routes import templates as templates_mod
        monkeypatch.setattr(templates_mod, "push_template_to_repo", lambda *a, **k: True)
        monkeypatch.setattr("src.domains.mgmt.routes.repos.host_repo",
                            lambda body, user, provider="": {
                                "message": "仓库托管成功", "webhook_id": 1})
        r = client.post("/api/project-templates/create",
                        json={"template_id": "tpl-3563", "repo_name": "repo-3563",
                              "provider": "github"})
        assert r.status_code == 200, r.text
        assert r.json()["full_name"] == "gh_user_3563/repo-3563"
        # 身份与建仓两次调用看到的 token 一致（同一生效 token 贯穿链路）
        assert seen_tokens[0] == seen_tokens[1]

    def test_create_repo_5xx_sanitized_502(self, monkeypatch, patched_refresh_failure):
        """建仓 5xx：502 语义保留 + 消息脱敏（剥内部 URL）。"""
        self._fake_template(monkeypatch)
        from src.domains.mgmt.vcs import GitHubProvider
        monkeypatch.setattr(GitHubProvider, "verify_token",
                            lambda self_, token: {"login": "gh_user_3563"})

        def _create_boom(self_, token, name, description="", **kwargs):
            raise RuntimeError("GitHub API POST https://api.github.com/user/repos failed (500): boom")

        monkeypatch.setattr(GitHubProvider, "create_repo", _create_boom)
        r = client.post("/api/project-templates/create",
                        json={"template_id": "tpl-3563", "repo_name": "repo-3563",
                              "provider": "github"})
        assert r.status_code == 502, r.text
        msg = r.json()["detail"]["message"]
        assert "创建仓库失败" in msg
        assert "api.github.com" not in r.text

    def test_create_repo_conflict_reuses_repo(self, monkeypatch, patched_refresh_failure):
        """同名冲突（422/already exists）→ 复用仓库继续 push/host → 200（冲突检测语义不变）。"""
        self._fake_template(monkeypatch)
        from src.domains.mgmt.vcs import GitHubProvider
        monkeypatch.setattr(GitHubProvider, "verify_token",
                            lambda self_, token: {"login": "gh_user_3563"})

        def _conflict(self_, token, name, description="", **kwargs):
            raise RuntimeError("GitHub API POST /user/repos failed (422): already exists")

        monkeypatch.setattr(GitHubProvider, "create_repo", _conflict)
        from src.domains.mgmt.routes import templates as templates_mod
        monkeypatch.setattr(templates_mod, "push_template_to_repo", lambda *a, **k: True)
        monkeypatch.setattr("src.domains.mgmt.routes.repos.host_repo",
                            lambda body, user, provider="": {
                                "message": "仓库托管成功", "webhook_id": 2})
        r = client.post("/api/project-templates/create",
                        json={"template_id": "tpl-3563", "repo_name": "repo-3563",
                              "provider": "github"})
        assert r.status_code == 200, r.text


# ─────────────────────────── 入口 3：托管权限判断 ───────────────────────────

class TestHostedPermissionReauth:
    def test_ensure_vcs_maintainer_401_detail_contract(self, monkeypatch):
        """_ensure_vcs_maintainer（repos 托管权限判断）：上游 401 + 刷新失败 → 401 reauth 三字段。"""
        user = _mk_github_oauth_user()
        try:
            def _login_401(provider, token):
                raise RuntimeError("GitHub API GET /user failed (401): Bad credentials")

            monkeypatch.setattr("src.domains.mgmt.user.repo_manager.vcs_user_login", _login_401)
            monkeypatch.setattr("src.api.deps._try_refresh_user_token",
                                lambda user_id, provider: None)
            monkeypatch.setattr("src.domains.mgmt.user.db.refresh_oauth_token",
                                lambda user_id, provider: None)
            from src.domains.mgmt.routes.repos import _ensure_vcs_maintainer
            with pytest.raises(HTTPException) as ei:
                _ensure_vcs_maintainer(user, "github", "gh_user_3563", "some-repo",
                                       "host", "rate limit msg")
            assert ei.value.status_code == 401
            detail = ei.value.detail
            assert detail.get("action") == "reauth_required"
            assert detail.get("provider") == "github"
            assert detail.get("message") == "GitHub 授权已过期，请重新登录授权"
            assert "error_code" not in detail
        finally:
            _cleanup(user)

    def test_host_repo_gitcode_identity_401_detail_contract(self, monkeypatch):
        """host_repo GitCode 托管身份段（repos.py 内联 with_oauth_token）：
        上游 401 + 刷新失败 → 401 reauth（detail 带 provider，统一出口生效）。"""
        from src.domains.mgmt.auth.grape_account_db import (
            create_external_identity, create_grape_account)
        uname = "s3563c_" + uuid.uuid4().hex[:6]
        u = create_grape_account(username=uname, email=f"{uname}@test.local", password="pw123")
        uid = str(u["id"])
        ok = create_external_identity(
            grape_user_id=uid, provider="gitcode",
            external_id="gc3563_" + uuid.uuid4().hex[:6],
            external_login=uname, access_token="expired_gc_tok", refresh_token="rt_gc")
        assert ok
        try:
            def _get_user_401(method, url, token="", body=None):
                raise RuntimeError("GitCode API GET https://gitcode.com/api/v5/user failed (401): unauthorized")

            monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _get_user_401)
            # 过 host_repo 前置的 webhook_url 配置检查（与 test_repos_host_provider 同款 mock）
            monkeypatch.setattr("src.domains.mgmt.user.db.get_webhook_url",
                                lambda: "https://grape.example.com/webhook/gitcode")
            monkeypatch.setattr("src.api.deps._try_refresh_user_token",
                                lambda user_id, provider: None)
            monkeypatch.setattr("src.domains.mgmt.user.db.refresh_oauth_token",
                                lambda user_id, provider: None)
            # host_repo 的身份探测经 routes.repos 内联 with_oauth_token → _req
            app.dependency_overrides[require_no_session_token] = lambda: {
                "id": uid, "username": uname, "login_provider": "gitcode"}
            try:
                r = client.post("/api/repos/host?provider=gitcode",
                                json={"full_name": f"{uname}/some-repo"})
                assert r.status_code == 401, r.text
                detail = r.json()["detail"]
                assert detail.get("action") == "reauth_required"
                assert detail.get("provider") == "gitcode"
                assert detail.get("message") == "GitCode 授权已过期，请重新登录授权"
                assert "error_code" not in detail
                assert "api.gitcode.com" not in r.text and "gitcode.com/api" not in r.text
            finally:
                app.dependency_overrides.pop(require_no_session_token, None)
        finally:
            with db_lock:
                db.execute("DELETE FROM external_identities WHERE grape_user_id=? AND provider=?", (uid, "gitcode"))
                db.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
                db.commit()


# ─────────────────────── repo_manager 状态码透出变体 ───────────────────────

class TestRepoManagerLoginStatus:
    def test_gitcode_login_status_success(self, monkeypatch):
        from src.domains.mgmt.user import repo_manager as rm
        monkeypatch.setattr(rm, "_req", lambda *a, **k: {"login": "octocat"})
        assert rm.get_user_gitcode_login_status("tok") == ("octocat", 0)
        assert rm.get_user_gitcode_login("tok") == "octocat"

    def test_gitcode_login_status_upstream_401(self, monkeypatch):
        from src.domains.mgmt.user import repo_manager as rm
        monkeypatch.setattr(rm, "_req", lambda *a, **k: (_ for _ in ()).throw(
            rm.UpstreamAPIError("GET", "https://gitcode.com/api/v5/user", 401, "Bad credentials")))
        assert rm.get_user_gitcode_login_status("tok") == ("", 401)
        assert rm.get_user_gitcode_login("tok") == ""

    def test_gitcode_login_status_network_error(self, monkeypatch):
        from src.domains.mgmt.user import repo_manager as rm
        monkeypatch.setattr(rm, "_req", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("Cannot reach GitCode API: boom")))
        assert rm.get_user_gitcode_login_status("tok") == ("", -1)
        assert rm.get_user_gitcode_login("tok") == ""
        assert rm.get_user_gitcode_login("") == ""

    def test_github_login_status_success(self, monkeypatch):
        from src.domains.mgmt.user import repo_manager as rm
        monkeypatch.setattr(rm, "_github_req", lambda *a, **k: {"login": "octocat"})
        assert rm.get_user_github_login_status("tok") == ("octocat", 0)
        assert rm.get_user_github_login("tok") == "octocat"

    def test_github_login_status_upstream_401(self, monkeypatch):
        from src.domains.mgmt.user import repo_manager as rm
        monkeypatch.setattr(rm, "_github_req", lambda *a, **k: (_ for _ in ()).throw(
            rm.UpstreamAPIError("GET", "https://api.github.com/user", 401, "Bad credentials",
                                provider="GitHub")))
        assert rm.get_user_github_login_status("tok") == ("", 401)
        assert rm.get_user_github_login("tok") == ""

    def test_github_login_status_network_error(self, monkeypatch):
        from src.domains.mgmt.user import repo_manager as rm
        monkeypatch.setattr(rm, "_github_req", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("Cannot reach GitHub API: boom")))
        assert rm.get_user_github_login_status("tok") == ("", -1)
        assert rm.get_user_github_login("") == ""


# ─────────────────────── oauth_token 统一出口 helper 契约 ───────────────────────

class TestOauthTokenHelpers:
    def test_reauth_required_detail_contract(self):
        from src.api.oauth_token import reauth_required_detail
        d = reauth_required_detail("github")
        assert d == {"code": 401, "message": "GitHub 授权已过期，请重新登录授权",
                     "action": "reauth_required", "provider": "github"}
        assert "error_code" not in d
        assert reauth_required_detail("gitcode")["message"] == "GitCode 授权已过期，请重新登录授权"
        assert reauth_required_detail("")["message"].endswith("授权已过期，请重新登录授权")

    def test_raise_reauth_required(self):
        from src.api.oauth_token import raise_reauth_required
        with pytest.raises(HTTPException) as ei:
            raise_reauth_required("gitcode")
        assert ei.value.status_code == 401
        assert ei.value.detail.get("action") == "reauth_required"
        assert ei.value.detail.get("provider") == "gitcode"

    def test_sanitize_upstream_error_strips_url(self):
        from src.api.oauth_token import sanitize_upstream_error
        raw = "GitHub API GET https://api.github.com/user failed (401): Bad credentials"
        out = sanitize_upstream_error(raw)
        assert "https" not in out and "api.github.com" not in out
        assert "(401)" in out and "Bad credentials" in out
        assert sanitize_upstream_error(None) == ""
        assert sanitize_upstream_error("") == ""
