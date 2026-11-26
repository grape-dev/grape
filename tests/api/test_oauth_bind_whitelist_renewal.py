"""OAuth 绑定白名单闸的续期豁免（Issue #4254 收口回归）。

覆盖 `_bind_whitelist_allows` 的三条放行口径与 `_handle_oauth_bind` 的接线：
- 本次身份自身命中白名单（原有行为）；
- 该 external_id 已在本账号名下（status 0/1/4）：token 过期后重新授权不是新绑定；
- 本账号任一已绑身份命中白名单（与 /api/auth/beta-eligibility 同口径）。
并守住反向：全新身份 + 账号整体无资格时仍按 WHITELIST 拒绝，且不触发绑定写入；
读失败（异常 / getter 吞异常返回 None 的未知态）一律 fail-closed 拒绝。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from starlette.requests import Request


def _fake_request(path="/api/auth/callback/gitcode"):
    return Request({
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "headers": [(b"host", b"testserver")], "query_string": b"",
        "server": ("testserver", 80), "scheme": "http", "client": ("127.0.0.1", 1234),
        "root_path": "",
    })


def _patch_gate(monkeypatch, whitelisted=False):
    from src.domains.mgmt.routes import grape_auth
    monkeypatch.setattr(grape_auth, "_check_whitelist", lambda p, l: whitelisted)


# ---------------------------------------------------------------- helper level

def test_whitelisted_login_is_allowed_without_identity_read(monkeypatch):
    """身份本身命中白名单时直接放行（不依赖任何身份查询）。"""
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db

    _patch_gate(monkeypatch, whitelisted=True)

    def _unexpected(*a, **kw):
        raise AssertionError("whitelisted identity must not trigger identity lookups")

    monkeypatch.setattr(grape_account_db, "get_external_identity_any_status", _unexpected)
    assert grape_auth._bind_whitelist_allows("gitcode", "ext-1", "bob", "u-1") is True


def test_own_expired_identity_renewal_is_allowed(monkeypatch):
    """status=4 的自有身份重新授权 = 续期，不是新绑定。"""
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db

    _patch_gate(monkeypatch, whitelisted=False)
    monkeypatch.setattr(
        grape_account_db, "get_external_identity_any_status",
        lambda p, e: {"grape_user_id": "u-1", "status": 4})
    assert grape_auth._bind_whitelist_allows("gitee", "ext-1", "bob", "u-1") is True


def test_other_users_identity_is_not_exempted(monkeypatch):
    """他人名下的身份不得借续期口径放行。"""
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db
    from src.domains.mgmt.auth import signup_policy

    _patch_gate(monkeypatch, whitelisted=False)
    monkeypatch.setattr(
        grape_account_db, "get_external_identity_any_status",
        lambda p, e: {"grape_user_id": "u-other", "status": 4})
    monkeypatch.setattr(grape_account_db, "get_grape_account_by_id",
                        lambda uid: {"id": uid, "username": "bob"})
    monkeypatch.setattr(signup_policy, "is_beta_whitelisted_user", lambda user: False)
    assert grape_auth._bind_whitelist_allows("gitee", "ext-1", "bob", "u-1") is False


def test_beta_eligible_account_binds_new_identity(monkeypatch):
    """账号经其它平台命中白名单（beta-eligibility 同口径）时可绑新身份。"""
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db
    from src.domains.mgmt.auth import signup_policy

    _patch_gate(monkeypatch, whitelisted=False)
    monkeypatch.setattr(grape_account_db, "get_external_identity_any_status",
                        lambda p, e: None)
    monkeypatch.setattr(grape_account_db, "get_grape_account_by_id",
                        lambda uid: {"id": uid, "username": "bob"})
    monkeypatch.setattr(signup_policy, "is_beta_whitelisted_user", lambda user: True)
    assert grape_auth._bind_whitelist_allows("gitee", "ext-new", "bob", "u-1") is True


def test_own_unbound_identity_rebind_is_allowed(monkeypatch):
    """status=0（解绑过）的自有身份重新绑定同样算恢复，不走白名单。

    一人一平台一行（UNIQUE(grape_user_id, provider)），解绑行由
    IdentityLifecycleService.bind 复用回写，因此 re-bind 不是新绑定。
    """
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db

    _patch_gate(monkeypatch, whitelisted=False)
    monkeypatch.setattr(
        grape_account_db, "get_external_identity_any_status",
        lambda p, e: {"grape_user_id": "u-1", "status": 0})
    assert grape_auth._bind_whitelist_allows("gitee", "ext-1", "bob", "u-1") is True


def test_identity_lookup_exception_is_refused(monkeypatch):
    """身份读取抛异常 → fail-closed 拒绝（本闸不是安全边界，但未知 ≠ 放行）。"""
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db

    _patch_gate(monkeypatch, whitelisted=False)

    def _boom(p, e):
        raise RuntimeError("db down")

    monkeypatch.setattr(grape_account_db, "get_external_identity_any_status", _boom)
    assert grape_auth._bind_whitelist_allows("gitee", "ext-1", "bob", "u-1") is False


def test_unknown_read_returns_none_is_refused(monkeypatch):
    """getter 自身吞异常返回 None 的未知态：身份/账号都读不出来 → 拒绝。

    真实链路里 `get_external_identity_any_status` 与 `get_grape_account_by_id`
    都 catch 后返回 None（identity_lifecycle.get_any_status、
    grape_account_db.get_grape_account_by_id），所以不能只钉 raise 分支。
    """
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db, signup_policy

    _patch_gate(monkeypatch, whitelisted=False)
    monkeypatch.setattr(grape_account_db, "get_external_identity_any_status",
                        lambda p, e: None)
    monkeypatch.setattr(grape_account_db, "get_grape_account_by_id", lambda uid: None)

    def _unexpected(user):
        raise AssertionError("unknown account must not be judged for eligibility")

    monkeypatch.setattr(signup_policy, "is_beta_whitelisted_user", _unexpected)
    assert grape_auth._bind_whitelist_allows("gitee", "ext-1", "bob", "u-1") is False


# ---------------------------------------------------------------- route level

def _patch_bind_writes(monkeypatch):
    from types import SimpleNamespace
    from src.domains.mgmt.routes import grape_auth

    written = {}

    def _fake_create(**kw):
        written.update(kw)
        return True

    # 路由里用的是模块级已绑定名（grape_auth.create_external_identity），
    # 必须 patch 在 grape_auth 上，否则会真的写测试库。
    monkeypatch.setattr(grape_auth, "create_external_identity", _fake_create)
    monkeypatch.setattr(grape_auth, "_sync_um_platform_col", lambda *a, **k: None)
    monkeypatch.setattr(grape_auth, "_clear_user_repos_cache", lambda *a, **k: None)
    monkeypatch.setattr(grape_auth, "_hook_platform_link", lambda *a, **k: None)
    monkeypatch.setattr(grape_auth, "auth_audit",
                        SimpleNamespace(log_audit=lambda *a, **k: None))
    return written


def test_bind_renewal_reaches_write_path(monkeypatch):
    """status=4 自有身份重新授权：闸放行，绑定写入照常发生（症状回归）。"""
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db, signup_policy

    _patch_gate(monkeypatch, whitelisted=False)
    monkeypatch.setattr(grape_account_db, "get_external_identity_any_status",
                        lambda p, e: {"grape_user_id": "u-1", "status": 4})
    monkeypatch.setattr(grape_account_db, "get_grape_account_by_id",
                        lambda uid: {"id": uid, "username": "bob"})
    monkeypatch.setattr(signup_policy, "is_beta_whitelisted_user", lambda user: True)
    monkeypatch.setattr(grape_auth, "get_external_identity", lambda p, e: None)
    monkeypatch.setattr(grape_auth, "get_external_identity_any_status",
                        lambda p, e: {"grape_user_id": "u-1", "status": 4})
    written = _patch_bind_writes(monkeypatch)

    resp = grape_auth._handle_oauth_bind(
        _fake_request(), "gitee", "ext-1",
        {"external_login": "bob", "external_name": "Bob"}, "tok", "ref", "u-1")
    assert resp.status_code == 302
    assert "bind_success=gitee" in resp.headers["location"]
    assert written.get("provider") == "gitee" and written.get("external_id") == "ext-1"


def test_new_identity_without_eligibility_still_refused(monkeypatch):
    """全新身份 + 账号整体无资格：仍按 WHITELIST 拒绝，且不进入绑定写入。"""
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.auth import grape_account_db, signup_policy

    _patch_gate(monkeypatch, whitelisted=False)
    monkeypatch.setattr(grape_account_db, "get_external_identity_any_status",
                        lambda p, e: None)
    monkeypatch.setattr(grape_account_db, "get_grape_account_by_id",
                        lambda uid: {"id": uid, "username": "bob"})
    monkeypatch.setattr(signup_policy, "is_beta_whitelisted_user", lambda user: False)

    def _unexpected(*a, **kw):
        raise AssertionError("refused bind must not reach identity lookups/writes")

    monkeypatch.setattr(grape_auth, "get_external_identity", _unexpected)
    monkeypatch.setattr(grape_auth, "create_external_identity", _unexpected)

    resp = grape_auth._handle_oauth_bind(
        _fake_request(), "gitee", "ext-new",
        {"external_login": "bob", "external_name": "Bob"}, "tok", "ref", "u-1")
    assert resp.status_code == 302
    assert "oauth_error=WHITELIST" in resp.headers["location"]