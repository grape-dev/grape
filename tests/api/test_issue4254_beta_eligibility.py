"""Issue #4254: 内测资格判定（自注册用户 OAuth/PAT 场景内测提示）。

覆盖 `check_beta_eligibility` 的判定分支与 `/api/auth/beta-eligibility` 路由：
- OAuth 注册用户恒不提示；
- login_whitelist 启用时按绑定身份命中；
- login_whitelist 关闭时按 signup_whitelist 邮箱/域名命中；
- 白名单为空 = 无管控（不提示）；
- 查询异常不误提示；
- /auth/profile 返回 created_by_oauth。
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.domains.mgmt.auth import signup_policy

client = TestClient(app)


def _eligibility(monkeypatch, account, identities=None,
                 login_enabled=False, login_allowed=False,
                 email_wl=None, domain_wl=None):
    monkeypatch.setattr(signup_policy, "_is_login_whitelist_enabled", lambda: login_enabled)
    monkeypatch.setattr(signup_policy, "is_user_allowed", lambda p, l: login_allowed)
    monkeypatch.setattr(signup_policy, "_get_whitelist_emails",
                        lambda: None if email_wl == "ERR" else (email_wl or set()))
    monkeypatch.setattr(signup_policy, "_get_whitelist_domains",
                        lambda: None if domain_wl == "ERR" else (domain_wl or set()))
    return signup_policy.check_beta_eligibility(account, identities)


def test_oauth_registered_user_never_prompted(monkeypatch):
    r = _eligibility(monkeypatch, {"created_by_oauth": 1, "email": "a@b.com"},
                     login_enabled=True, login_allowed=False)
    assert r["is_self_registered"] is False
    assert r["whitelisted"] is True


def test_login_whitelist_enabled_identity_hit(monkeypatch):
    r = _eligibility(
        monkeypatch,
        {"created_by_oauth": 0, "email": "a@b.com"},
        identities=[{"provider": "gitcode", "external_login": "alice"}],
        login_enabled=True,
        login_allowed=True,
    )
    assert r["is_self_registered"] is True
    assert r["whitelisted"] is True
    assert r["apply_url"] == "/beta-apply"


def test_login_whitelist_enabled_no_hit_prompts(monkeypatch):
    r = _eligibility(
        monkeypatch,
        {"created_by_oauth": 0, "email": "a@b.com"},
        identities=[{"provider": "gitcode", "external_login": "bob"}],
        login_enabled=True,
        login_allowed=False,
    )
    assert r["whitelisted"] is False


def test_login_whitelist_enabled_without_identity_prompts(monkeypatch):
    r = _eligibility(monkeypatch, {"created_by_oauth": 0, "email": "a@b.com"},
                     identities=[], login_enabled=True, login_allowed=True)
    assert r["whitelisted"] is False


def test_signup_whitelist_email_hit(monkeypatch):
    r = _eligibility(monkeypatch, {"created_by_oauth": 0, "email": "a@b.com"},
                     email_wl={"a@b.com"})
    assert r["whitelisted"] is True


def test_signup_whitelist_domain_hit(monkeypatch):
    r = _eligibility(monkeypatch, {"created_by_oauth": 0, "email": "a@b.com"},
                     domain_wl={"b.com"})
    assert r["whitelisted"] is True


def test_signup_whitelist_nonempty_no_hit_prompts(monkeypatch):
    r = _eligibility(monkeypatch, {"created_by_oauth": 0, "email": "c@d.com"},
                     email_wl={"a@b.com"})
    assert r["whitelisted"] is False


def test_no_whitelist_control_no_prompt(monkeypatch):
    r = _eligibility(monkeypatch, {"created_by_oauth": 0, "email": ""})
    assert r["whitelisted"] is True


def test_signup_whitelist_query_error_no_prompt(monkeypatch):
    r = _eligibility(monkeypatch, {"created_by_oauth": 0, "email": "a@b.com"},
                     email_wl="ERR", domain_wl="ERR")
    assert r["whitelisted"] is True


def test_check_exception_no_prompt(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(signup_policy, "_is_login_whitelist_enabled", _boom)
    r = signup_policy.check_beta_eligibility(
        {"created_by_oauth": 0, "email": "a@b.com"}, [])
    assert r["whitelisted"] is True


def _auth_headers(created_by_oauth=0):
    from src.domains.mgmt.auth.grape_account_db import (
        create_grape_account, get_grape_account_by_username,
    )
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version

    uname = "beta_" + uuid.uuid4().hex[:8]
    u = create_grape_account(username=uname, email=f"{uname}@example.com", password="pw")
    if created_by_oauth:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE grape_accounts SET created_by_oauth=1 WHERE id=?", (u["id"],))
            db.commit()
    tok = create_access_token(u["id"], u["username"], get_token_version(u["id"]))
    return u, {"Authorization": f"Bearer {tok}"}


def test_api_route_self_registered_prompts(monkeypatch):
    monkeypatch.setattr(signup_policy, "_is_login_whitelist_enabled", lambda: False)
    monkeypatch.setattr(signup_policy, "_signup_whitelist_match", lambda email: False)
    u, h = _auth_headers(created_by_oauth=0)
    try:
        r = client.get("/api/auth/beta-eligibility", headers=h)
        assert r.status_code == 200
        data = r.json()
        assert data["is_self_registered"] is True
        assert data["whitelisted"] is False
        assert data["apply_url"] == "/beta-apply"
    finally:
        from src.domains.mgmt.auth.grape_account_db import soft_delete_account
        soft_delete_account(u["id"])


def test_api_route_oauth_user_no_prompt(monkeypatch):
    monkeypatch.setattr(signup_policy, "_is_login_whitelist_enabled", lambda: False)
    u, h = _auth_headers(created_by_oauth=1)
    try:
        r = client.get("/api/auth/beta-eligibility", headers=h)
        assert r.status_code == 200
        data = r.json()
        assert data["is_self_registered"] is False
        assert data["whitelisted"] is True
    finally:
        from src.domains.mgmt.auth.grape_account_db import soft_delete_account
        soft_delete_account(u["id"])


def test_api_route_requires_auth():
    assert client.get("/api/auth/beta-eligibility").status_code == 401


def test_profile_returns_created_by_oauth():
    u, h = _auth_headers(created_by_oauth=1)
    try:
        r = client.get("/api/auth/profile", headers=h)
        assert r.status_code == 200
        assert r.json()["created_by_oauth"] == 1
    finally:
        from src.domains.mgmt.auth.grape_account_db import soft_delete_account
        soft_delete_account(u["id"])