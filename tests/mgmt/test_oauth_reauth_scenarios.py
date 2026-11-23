"""场景用例代码：OAuth 令牌过期续期与三入口 401 重授权分流（Issue #3563，S1–S10）。

对应用例表：`docs/weekly-11/oauth-reauth-token-refresh-design.md` §5。
每个场景是一条**用户旅程**（不是单个 helper 的单元测试）：过期令牌 → 自动续期 →
续期不可用 → 统一 401 → 前端可分流；另有正常令牌与上游非令牌错误的回归。

真机限制：本机 GitHub OAuth 渠道凭据为空、无 github 绑定身份，上游行为用 mock 替代。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import require_no_session_token, require_org_member
from src.kernel.session_manager import db, db_lock

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]
OLD = "expired_tok_3563"
NEW = "refreshed_tok_3563"


def _mk_github_user() -> dict:
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, create_grape_account)

    uname = "s3563s_" + uuid.uuid4().hex[:6]
    u = create_grape_account(username=uname, email=f"{uname}@test.local", password="pw123")
    ok = create_external_identity(
        grape_user_id=str(u["id"]), provider="github",
        external_id="gh3563s_" + uuid.uuid4().hex[:6], external_login=uname,
        access_token=OLD, refresh_token="rt_3563", expires_in=60)
    assert ok
    return {"id": str(u["id"]), "username": uname}


def _cleanup(user: dict) -> None:
    with db_lock:
        db.execute("DELETE FROM external_identities WHERE grape_user_id=? AND provider=?",
                   (user["id"], "github"))
        db.execute("DELETE FROM grape_accounts WHERE id=?", (user["id"],))
        db.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (user["username"],))
        db.commit()


def _wire(monkeypatch, user: dict, *, refresh_ok: bool):
    """统一接线：当前 token 取环境里的 OLD；刷新结果可控；记录实际使用的 token。"""
    import src.api.deps as deps
    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(deps, "get_user_token_for",
                        lambda username, user_id="", provider="github": OLD)
    monkeypatch.setattr(udb, "refresh_oauth_token",
                        lambda user_id, provider: NEW if refresh_ok else None)


@pytest.fixture()
def user():
    u = _mk_github_user()
    app.dependency_overrides[require_no_session_token] = lambda: {**u, "login_provider": "github"}
    yield u
    app.dependency_overrides.pop(require_no_session_token, None)
    _cleanup(u)


def _github_verify(monkeypatch, *, accept_token: str, login: str = "gh_user_3563"):
    """verify_token：只有 accept_token 能拿到 login，其它一律上游 401。"""
    from src.domains.mgmt.vcs import GitHubProvider
    seen: list[str] = []

    def _verify(self_, token):
        seen.append(token)
        if token != accept_token:
            raise RuntimeError("GitHub API GET /user failed (401): Bad credentials")
        return {"login": login}

    monkeypatch.setattr(GitHubProvider, "verify_token", _verify)
    return seen


# ─────────────────────── S1：过期 + 刷新可用 → check-name 正常 ───────────────────────

def test_s1_expired_token_refreshes_on_check_name(user, monkeypatch):
    _wire(monkeypatch, user, refresh_ok=True)
    seen = _github_verify(monkeypatch, accept_token=NEW)          # 旧 token 必然 401
    from src.domains.runtime.vcs.api_client import VcsApiClient
    used: list[str] = []

    def _repo_info(self_):
        used.append(getattr(self_, "token", ""))
        return {"id": 1, "full_name": "gh_user_3563/some-repo"}

    monkeypatch.setattr(VcsApiClient, "get_repo_info", _repo_info)

    r = client.get("/api/repos/check-name", params={"name": "some-repo"})
    assert r.status_code == 200, r.text
    assert r.json()["exists"] is True
    assert seen[:2] == [OLD, NEW], f"应先用旧 token 失败一次、再用刷新后的新 token: {seen}"
    assert used and used[0] == NEW, f"存在性复查必须用刷新后的 token: {used}"


# ──────────────────── S2：过期 + 刷新可用 → 托管权限判断不 401 ────────────────────

def test_s2_expired_token_refreshes_on_hosted_permission(user, monkeypatch):
    _wire(monkeypatch, user, refresh_ok=True)
    import src.domains.mgmt.user.repo_manager as rm
    seen: list[str] = []

    def _login(provider, token):
        seen.append(token)
        if token != NEW:
            raise RuntimeError("GitHub API GET /user failed (401): Bad credentials")
        return "other-user"

    monkeypatch.setattr(rm, "vcs_user_login", _login)
    monkeypatch.setattr(rm, "vcs_user_permission",
                        lambda provider, token, owner, repo, login: {"is_maintainer": True})
    from src.domains.mgmt.routes.repos import _ensure_vcs_maintainer

    _ensure_vcs_maintainer({"id": user["id"], "username": user["username"]},
                           "github", "some-owner", "some-repo", "托管", "rate limit")
    assert seen[:2] == [OLD, NEW], seen


# ────────────── S3/S4/S5：刷新不可用 → 三入口统一 401 reauth ──────────────

def _assert_reauth(r, provider: str = "github"):
    assert r.status_code == 401, r.text
    d = json.loads(r.text)["detail"]
    assert d["action"] == "reauth_required"
    assert d["provider"] == provider
    assert d["message"] == "GitHub 授权已过期，请重新登录授权"
    assert "error_code" not in d
    assert "api.github.com" not in r.text


def test_s3_check_name_401_after_refresh_failure(user, monkeypatch):
    _wire(monkeypatch, user, refresh_ok=False)
    _github_verify(monkeypatch, accept_token="never")
    _assert_reauth(client.get("/api/repos/check-name", params={"name": "x"}))
    from src.domains.mgmt.auth.grape_account_db import is_identity_expired
    assert is_identity_expired(user["id"], "github") is True, "失败链应落 status=4"


def test_s4_hosted_permission_401_after_refresh_failure(user, monkeypatch):
    _wire(monkeypatch, user, refresh_ok=False)
    import src.domains.mgmt.user.repo_manager as rm

    def _login(provider, token):
        raise RuntimeError("GitHub API GET /user failed (401): Bad credentials")

    monkeypatch.setattr(rm, "vcs_user_login", _login)
    from src.domains.mgmt.routes.repos import _ensure_vcs_maintainer
    with pytest.raises(HTTPException) as ei:
        _ensure_vcs_maintainer({"id": user["id"], "username": user["username"]},
                               "github", "some-owner", "some-repo", "托管", "rate limit")
    _assert_reauth_detail(ei.value.detail)


def _assert_reauth_detail(d: dict, provider: str = "github"):
    assert d["code"] == 401 and d["action"] == "reauth_required"
    assert d["provider"] == provider
    assert d["message"] == "GitHub 授权已过期，请重新登录授权"
    assert "error_code" not in d
    assert "api.github.com" not in json.dumps(d)


def test_s5_template_create_401_after_refresh_failure(monkeypatch):
    u = _mk_github_user()
    app.dependency_overrides[require_org_member] = lambda: {
        **u, "org_id": "", "org_role": "owner", "login_provider": "github"}
    try:
        _wire(monkeypatch, u, refresh_ok=False)
        _github_verify(monkeypatch, accept_token="never")
        from src.domains.mgmt.routes import templates as tm
        monkeypatch.setattr(tm, "_find_template", lambda tid: {
            "template": {"id": "tpl-3563", "source": "local", "scene_id": "",
                         "default_repo_name": "repo-3563", "description": "d"},
            "scene_id": ""})
        r = client.post("/api/project-templates/create",
                        json={"template_id": "tpl-3563", "repo_name": "repo-3563",
                              "provider": "github"})
        _assert_reauth(r)
    finally:
        app.dependency_overrides.pop(require_org_member, None)
        _cleanup(u)


# ─────────────────── S6/S7/S8：非 401 面与正常令牌回归 ───────────────────

def test_s6_missing_login_keeps_identity_502(user, monkeypatch):
    _wire(monkeypatch, user, refresh_ok=True)
    _github_verify(monkeypatch, accept_token=OLD, login="")       # 成功但无 login
    r = client.get("/api/repos/check-name", params={"name": "x"})
    assert r.status_code == 502, r.text
    assert "无法获取当前用户身份" in r.json()["detail"]["message"]


def test_s7_upstream_5xx_is_sanitized(user, monkeypatch):
    _wire(monkeypatch, user, refresh_ok=True)
    _github_verify(monkeypatch, accept_token=OLD)
    from src.domains.runtime.vcs.api_client import VcsApiClient
    monkeypatch.setattr(VcsApiClient, "get_repo_info", lambda self_: {
        "error": "GitHub API GET https://api.github.com/repos/o/r failed (500): boom"})
    r = client.get("/api/repos/check-name", params={"name": "x"})
    assert r.status_code == 502, r.text
    msg = r.json()["detail"]["message"]
    assert "(500)" in msg and "api.github.com" not in msg


def test_s8_normal_token_returns_exists(user, monkeypatch):
    _wire(monkeypatch, user, refresh_ok=True)
    _github_verify(monkeypatch, accept_token=OLD)
    from src.domains.runtime.vcs.api_client import VcsApiClient
    import src.domains.runtime.vcs.api_client as acl
    monkeypatch.setattr(VcsApiClient, "get_repo_info",
                        lambda self_: {"id": 1, "full_name": "gh_user_3563/x"})
    r = client.get("/api/repos/check-name", params={"name": "x"})
    assert r.status_code == 200 and r.json()["exists"] is True

    monkeypatch.setattr(VcsApiClient, "get_repo_info", lambda self_: {"error": "HTTP 404"})
    r = client.get("/api/repos/check-name", params={"name": "x"})
    assert r.status_code == 200 and r.json()["exists"] is False


# ─────────────────────── S9/S10：前端与协议契约锁定 ───────────────────────

def test_s9_frontend_provider_routing_present():
    api = (ROOT / "src/api/frontend/shell/js/api.js").read_text(encoding="utf-8")
    assert "reauth_required" in api
    assert "detail.provider" in api or "detail?.provider" in api, \
        "api.js 必须按 provider 分流（Spec v2 B2）"
    for lang in ("zh", "en"):
        loc = json.loads((ROOT / f"src/api/frontend/shell/locales/{lang}/common.json")
                         .read_text(encoding="utf-8"))
        assert "github_reauth_required" in loc, f"{lang} 缺 github_reauth_required"
        assert "github_reauth_token_hint" in loc, f"{lang} 缺 github_reauth_token_hint"


def test_s10_protocol_has_no_error_code():
    from src.api import oauth_token as ot
    d = ot.reauth_required_detail("github")
    assert set(d) == {"code", "message", "action", "provider"}
    assert "error_code" not in d
    src = (ROOT / "src/api/oauth_token.py").read_text(encoding="utf-8")
    body = src.split("def with_oauth_token", 1)[1]
    assert body.count("raise_reauth_required(provider)") == 3, \
        "三处 401 出口必须统一（不得各自拼 detail）"
    assert '"action": "reauth_required"' not in body, "with_oauth_token 内不得再有内联 detail"