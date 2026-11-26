"""Issue #4254: 内测资格判定（自注册用户 OAuth/PAT 场景内测提示）。

覆盖 `check_beta_eligibility` 的判定分支与 `/api/auth/beta-eligibility` 路由：
- 判据只看白名单是否命中，与建号来源无关（OAuth 建号但未命中白名单同样提示）；
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


def test_oauth_registered_user_not_whitelisted_prompts(monkeypatch):
    """OAuth 建号但没命中白名单，同样要提示去申请内测（与建号来源无关）。"""
    r = _eligibility(monkeypatch, {"created_by_oauth": 1, "email": "a@b.com"},
                     identities=[{"provider": "gitcode", "external_login": "bob"}],
                     login_enabled=True, login_allowed=False)
    assert r["is_self_registered"] is False
    assert r["whitelisted"] is False


def test_oauth_registered_user_whitelisted_no_prompt(monkeypatch):
    r = _eligibility(monkeypatch, {"created_by_oauth": 1, "email": "a@b.com"},
                     identities=[{"provider": "gitcode", "external_login": "alice"}],
                     login_enabled=True, login_allowed=True)
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


def test_api_route_oauth_user_without_whitelist_prompts(monkeypatch):
    """线上实测口径：created_by_oauth=1 但未命中白名单的账号同样要提示。"""
    monkeypatch.setattr(signup_policy, "_is_login_whitelist_enabled", lambda: True)
    u, h = _auth_headers(created_by_oauth=1)
    try:
        r = client.get("/api/auth/beta-eligibility", headers=h)
        assert r.status_code == 200
        data = r.json()
        assert data["is_self_registered"] is False
        assert data["whitelisted"] is False
    finally:
        from src.domains.mgmt.auth.grape_account_db import soft_delete_account
        soft_delete_account(u["id"])


def test_api_route_identity_read_error_no_prompt(monkeypatch):
    """身份读异常 → 未知态，不得当成"无绑定未命中"误提示。"""
    monkeypatch.setattr(signup_policy, "_is_login_whitelist_enabled", lambda: True)

    import src.domains.mgmt.auth.grape_account_db as gadb

    def _boom(*args, **kwargs):
        raise RuntimeError("identity db down")

    monkeypatch.setattr(gadb, "get_user_identities", _boom)
    u, h = _auth_headers(created_by_oauth=0)
    try:
        r = client.get("/api/auth/beta-eligibility", headers=h)
        assert r.status_code == 200
        data = r.json()
        assert data["is_self_registered"] is True
        assert data["whitelisted"] is True
    finally:
        from src.domains.mgmt.auth.grape_account_db import soft_delete_account
        soft_delete_account(u["id"])


def test_api_route_account_read_error_no_prompt(monkeypatch):
    """账号读异常（读失败 ≠ 不存在）→ 直接按已具备资格处理，不误提示。"""
    import src.domains.mgmt.routes.auth as auth_routes

    def _boom(*args, **kwargs):
        raise RuntimeError("account db down")

    monkeypatch.setattr(auth_routes, "get_grape_account_by_username", _boom)
    u, h = _auth_headers(created_by_oauth=0)
    try:
        r = client.get("/api/auth/beta-eligibility", headers=h)
        assert r.status_code == 200
        data = r.json()
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

def test_api_credentials_blocked_for_non_whitelisted(monkeypatch):
    """未命中白名单用户不得保存凭据（Issue #4254 第二阶段：硬阻断）。"""
    monkeypatch.setattr(signup_policy, "_is_login_whitelist_enabled", lambda: True)
    u, h = _auth_headers(created_by_oauth=0)
    try:
        r = client.put("/api/settings/credentials", headers=h, json={})
        assert r.status_code == 403
        detail = r.json()["detail"]
        assert detail["error_code"] == "BETA_WHITELIST_REQUIRED"
        assert detail["apply_url"] == "/beta-apply"
    finally:
        from src.domains.mgmt.auth.grape_account_db import soft_delete_account
        soft_delete_account(u["id"])


def test_api_credentials_allowed_for_whitelisted(monkeypatch):
    """命中白名单用户不受门禁影响（门禁只挡未授权）。"""
    monkeypatch.setattr(signup_policy, "is_beta_whitelisted_user", lambda user: True)
    u, h = _auth_headers(created_by_oauth=1)
    try:
        r = client.put("/api/settings/credentials", headers=h, json={})
        assert r.status_code == 200
    finally:
        from src.domains.mgmt.auth.grape_account_db import soft_delete_account
        soft_delete_account(u["id"])


def _fake_request(path="/api/auth/callback/gitcode"):
    from starlette.requests import Request
    return Request({
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "headers": [(b"host", b"testserver")], "query_string": b"",
        "server": ("testserver", 80), "scheme": "http", "client": ("127.0.0.1", 1234),
        "root_path": "",
    })


def test_oauth_bind_refused_before_any_db_write(monkeypatch):
    """未命中白名单、且无续期/资格豁免的第三方身份不得建立绑定。

    拒绝必须发生在任何绑定写入与 status=1 绑定查询之前；`_bind_whitelist_allows`
    的续期/资格判定是只读查询，允许发生在闸内（见
    tests/api/test_oauth_bind_whitelist_renewal.py）。
    """
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db, signup_policy

    monkeypatch.setattr(grape_auth, "_check_whitelist", lambda p, l: False)
    monkeypatch.setattr(grape_account_db, "get_external_identity_any_status", lambda p, e: None)
    monkeypatch.setattr(grape_account_db, "get_grape_account_by_id", lambda uid: None)
    monkeypatch.setattr(signup_policy, "is_beta_whitelisted_user", lambda user: False)

    def _must_not_touch_db(*a, **kw):
        raise AssertionError("拒绝路径进入了绑定查询/写入")

    monkeypatch.setattr(grape_account_db, "get_external_identity", _must_not_touch_db)
    resp = grape_auth._handle_oauth_bind(
        _fake_request(), "gitcode", "ext-4254-1",
        {"external_login": "bob", "external_name": "Bob"}, "tok", "", "user-uuid", popup=False,
    )
    assert resp.status_code == 302
    assert "oauth_error=WHITELIST" in resp.headers["location"]
    assert "gitcode" in resp.headers["location"]


def test_oauth_bind_allowed_passes_the_gate(monkeypatch):
    """命中白名单的身份通过闸（后续 DB 行为由绑定流程自己负责）。"""
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db

    monkeypatch.setattr(grape_auth, "_check_whitelist", lambda p, l: True)
    reached = {}

    def _fake_get_external_identity(*a, **kw):
        reached["db"] = True
        return None

    monkeypatch.setattr(grape_account_db, "get_external_identity", _fake_get_external_identity)
    try:
        grape_auth._handle_oauth_bind(
            _fake_request(), "gitcode", "ext-4254-2",
            {"external_login": "alice", "external_name": "Alice"}, "tok", "", "user-uuid", popup=False,
        )
    except Exception:
        pass  # 后续写入用假 user id 必然失败，只要证明闸已放行到 DB 层即可
    assert reached.get("db") is True
