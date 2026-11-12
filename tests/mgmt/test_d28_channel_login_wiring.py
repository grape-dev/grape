"""D28（S48）用例：登录路由按策略能力分派接线（registry 查找，不比 type 字符串）。

全部为单元用例：fake Request + meta 卡 + mock 委托点，不触碰真实 IdP。
SAML 实弹件（验签钩子、时钟偏移、重放、ACS 绑定）归 IdP 联调服务线。
"""
import json

import pytest
from fastapi import HTTPException

from src.kernel.session_manager import db, db_lock


class _FakeRequest:
    """最小 Request 替身：headers/cookies/client 均为真实对象。"""

    def __init__(self, cookies=None, headers=None, client_host="127.0.0.1"):
        self.cookies = cookies or {}
        self.headers = headers or {"host": "127.0.0.1:8000"}
        self.client = type("C", (), {"host": client_host})()


def _set_providers_cfg(cfg: dict):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (json.dumps(cfg),))
        db.commit()
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    invalidate_provider_cache()


def _clear_providers_cfg():
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
        db.commit()
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    invalidate_provider_cache()


_SAML_CARD = {
    "type": "saml", "name": "HW SSO",
    "idp_sso_url": "https://idp.example.com/sso",
    "attribute_map": {"external_id": "uid", "login": "login"},
}


# ------------------------------------------------------- S48 发起：SAML 走策略层

def test_s48_saml_login_redirect_via_strategy():
    from src.domains.mgmt.routes.grape_auth import oauth_login
    from unittest.mock import patch

    _set_providers_cfg({"hw_sso": _SAML_CARD})
    try:
        with patch("src.domains.mgmt.auth.login_throttle.check_oauth_rate_limit", return_value=True), \
             patch("src.domains.mgmt.auth.providers.get_redirect_uri",
                   return_value="https://cb.example.com/api/auth/callback/hw_sso"), \
             patch("src.domains.mgmt.routes.grape_auth._get_request_lang", return_value="zh"):
            resp = oauth_login("hw_sso", request=_FakeRequest(cookies={}))
        assert resp.status_code == 200
        import json as _j
        body = _j.loads(resp.body)
        url = body["redirect_url"]
        assert url.startswith("https://idp.example.com/sso?RelayState=")
        # login flow → nonce cookie 下发（与 OAuth 同机制）
        setc = [v.decode() for k, v in resp.raw_headers if k.decode().lower() == "set-cookie"]
        assert any("oauth_nonce=" in c for c in setc)
        # state 已由策略落库：从 RelayState 取回并校验（单次消费）
        state = url.split("RelayState=")[1]
        from src.domains.mgmt.user.db import validate_oauth_state
        sd = validate_oauth_state(state)
        assert sd is not None and sd.get("_provider") == "hw_sso"
        assert sd.get("redirect_uri") == "https://cb.example.com/api/auth/callback/hw_sso"
        assert sd.get("login_flow") is True
    finally:
        _clear_providers_cfg()


def test_s48_oauth_login_legacy_regression():
    """存量 oauth2 渠道：策略委托 build_auth_url，行为零改动（S48 回归锁）。"""
    from src.domains.mgmt.routes.grape_auth import oauth_login
    from unittest.mock import patch

    with patch("src.domains.mgmt.auth.login_throttle.check_oauth_rate_limit", return_value=True), \
         patch("src.domains.mgmt.auth.providers._get_client_id", return_value="cid-1"), \
         patch("src.domains.mgmt.auth.providers.get_redirect_uri",
               return_value="https://cb.example.com/api/auth/callback/gitcode"), \
         patch("src.domains.mgmt.auth.providers.build_auth_url",
               return_value={"redirect_url": "https://gitcode.com/oauth/authorize?x=1"}) as ba, \
         patch("src.domains.mgmt.routes.grape_auth._get_request_lang", return_value="zh"):
        resp = oauth_login("gitcode", request=_FakeRequest(cookies={}))
    assert resp.status_code == 200
    ba.assert_called_once()                       # 委托存量机制，未被绕开
    import json as _j
    assert _j.loads(resp.body)["redirect_url"] == "https://gitcode.com/oauth/authorize?x=1"


def test_s48_saml_card_invalid_rejected_at_build():
    """缺 idp_sso_url 的 SAML 卡 → 构建时拒绝（S45 前置，报字段名）。"""
    from src.domains.mgmt.routes.grape_auth import oauth_login
    from unittest.mock import patch

    bad = {"type": "saml", "name": "HW SSO",
           "attribute_map": {"external_id": "uid"}}
    _set_providers_cfg({"hw_sso": bad})
    try:
        with patch("src.domains.mgmt.auth.login_throttle.check_oauth_rate_limit", return_value=True), \
             patch("src.domains.mgmt.routes.grape_auth._get_request_lang", return_value="zh"):
            with pytest.raises(HTTPException) as ei:
                oauth_login("hw_sso", request=_FakeRequest(cookies={}))
        assert ei.value.status_code == 400
        assert "idp_sso_url" in str(ei.value.detail)
    finally:
        _clear_providers_cfg()


# ------------------------------------------------------- S48 POST 回调（SAML ACS）

def test_s48_post_callback_gate_non_post_channel():
    """非 POST binding 渠道（gitcode/oauth2）打 POST ACS → 400 能力拒绝。"""
    from src.domains.mgmt.routes.grape_auth import saml_callback

    with pytest.raises(HTTPException) as ei:
        saml_callback(_FakeRequest(cookies={}), "gitcode",
                      SAMLResponse="b64", RelayState="st")
    assert ei.value.status_code == 400


def test_s48_post_callback_unverified_assertion_rejected():
    """验签钩子未装配（无真实 IdP 元数据）→ 安全默认拒绝登录。"""
    from src.domains.mgmt.routes.grape_auth import saml_callback
    from src.domains.mgmt.user.db import save_oauth_state

    _set_providers_cfg({"hw_sso": _SAML_CARD})
    try:
        save_oauth_state("st-uv", "hw_sso", {"redirect_uri": "https://cb/x"})
        resp = saml_callback(_FakeRequest(cookies={}), "hw_sso",
                             SAMLResponse="b64", RelayState="st-uv")
        assert resp.status_code == 302
        assert "saml_verify_failed" in str(resp.headers.get("location", ""))
    finally:
        _clear_providers_cfg()


def test_s48_post_callback_login_dispatch():
    """验签注入（mock）→ 归一声明走既有 _handle_oauth_login 分流（S43 复用）。"""
    from src.domains.mgmt.routes.grape_auth import saml_callback
    from src.domains.mgmt.user.db import save_oauth_state
    from unittest.mock import patch
    from src.kernel.auth_sdk.channel_strategy import SamlChannelStrategy

    _set_providers_cfg({"hw_sso": _SAML_CARD})
    try:
        # login flow state：nonce 与 cookie 匹配（公共校验链）
        save_oauth_state("st-ok", "hw_sso",
                         {"login_flow": True, "session_nonce": "n1",
                          "redirect_uri": "https://cb/x"})
        _verified = SamlChannelStrategy(
            verify_assertion=lambda b64, card: {"uid": "77", "login": "bob",
                                                "name": "Bob B"})
        with patch("src.kernel.auth_sdk.channel_strategy.get_channel_strategy",
                   return_value=_verified), \
             patch("src.domains.mgmt.routes.grape_auth._handle_oauth_login") as hlogin:
            hlogin.return_value.delete_cookie = lambda *a, **k: None
            resp = saml_callback(
                _FakeRequest(cookies={"oauth_nonce": "n1"}), "hw_sso",
                SAMLResponse="b64", RelayState="st-ok")
        hlogin.assert_called_once()
        args, kwargs = hlogin.call_args
        # (request, provider, external_id, user_info, access_token, refresh_token, ...)
        assert args[1] == "hw_sso" and args[2] == "77"
        assert args[3]["external_login"] == "bob" and args[3]["external_name"] == "Bob B"
        assert args[3]["external_id"] == "77" and args[3]["access_token"] == ""
        assert resp is hlogin.return_value
    finally:
        _clear_providers_cfg()


def test_s48_post_callback_nonce_mismatch_rejected():
    """login flow nonce 不匹配 → 公共校验链拒绝（与 GET 入口同一 helper）。"""
    from src.domains.mgmt.routes.grape_auth import saml_callback
    from src.domains.mgmt.user.db import save_oauth_state

    _set_providers_cfg({"hw_sso": _SAML_CARD})
    try:
        save_oauth_state("st-bad", "hw_sso",
                         {"login_flow": True, "session_nonce": "n1",
                          "redirect_uri": "https://cb/x"})
        resp = saml_callback(_FakeRequest(cookies={}), "hw_sso",   # 无 nonce cookie
                             SAMLResponse="b64", RelayState="st-bad")
        assert resp.status_code == 302
        assert "state_mismatch" in str(resp.headers.get("location", ""))
    finally:
        _clear_providers_cfg()


# ------------------------------------------------------- S48 能力契约（协议属性）

def test_s48_capability_matrix():
    """协议能力矩阵：oauth2/oidc 需 OAuth 应用、GET 回调；SAML 相反。"""
    from src.kernel.auth_sdk.channel_strategy import (OAuth2ChannelStrategy,
                                           OidcChannelStrategy,
                                           SamlChannelStrategy)
    o2, oidc, saml = OAuth2ChannelStrategy(), OidcChannelStrategy(), SamlChannelStrategy()
    assert o2.needs_oauth_client and not o2.post_callback
    assert oidc.needs_oauth_client and not oidc.post_callback
    assert not saml.needs_oauth_client and saml.post_callback


# ------------------------------------------------------- S48 POST 回调错误分支

def test_s48_post_callback_missing_response():
    from src.domains.mgmt.routes.grape_auth import saml_callback
    from src.domains.mgmt.user.db import save_oauth_state

    _set_providers_cfg({"hw_sso": _SAML_CARD})
    try:
        save_oauth_state("st-norsp", "hw_sso", {"redirect_uri": "https://cb/x"})
        resp = saml_callback(_FakeRequest(cookies={}), "hw_sso",
                             SAMLResponse="", RelayState="st-norsp")
        assert resp.status_code == 302
        assert "saml_response_missing" in str(resp.headers.get("location", ""))
    finally:
        _clear_providers_cfg()


def test_s48_post_callback_state_invalid():
    from src.domains.mgmt.routes.grape_auth import saml_callback

    _set_providers_cfg({"hw_sso": _SAML_CARD})
    try:
        resp = saml_callback(_FakeRequest(cookies={}), "hw_sso",
                             SAMLResponse="b64", RelayState="st-never-saved")
        assert resp.status_code == 302
        assert "state_invalid" in str(resp.headers.get("location", ""))
    finally:
        _clear_providers_cfg()


def test_s48_post_callback_provider_mismatch():
    """state 属于 hw_sso，URL 打 hw_sso2 → 公共校验链拒绝（provider 一致性）。"""
    from src.domains.mgmt.routes.grape_auth import saml_callback
    from src.domains.mgmt.user.db import save_oauth_state

    _set_providers_cfg({"hw_sso": _SAML_CARD,
                        "hw_sso2": dict(_SAML_CARD, idp_sso_url="https://idp2/sso")})
    try:
        save_oauth_state("st-pm", "hw_sso", {"redirect_uri": "https://cb/x"})
        resp = saml_callback(_FakeRequest(cookies={}), "hw_sso2",
                             SAMLResponse="b64", RelayState="st-pm")
        assert resp.status_code == 302
        assert "provider_mismatch" in str(resp.headers.get("location", ""))
    finally:
        _clear_providers_cfg()


def test_s48_post_callback_bind_dispatch():
    """state 携带 binding_user_id → 走既有 _handle_oauth_bind 分流（S43 复用）。"""
    from src.domains.mgmt.routes.grape_auth import saml_callback
    from src.domains.mgmt.user.db import save_oauth_state
    from unittest.mock import patch
    from src.kernel.auth_sdk.channel_strategy import SamlChannelStrategy

    _set_providers_cfg({"hw_sso": _SAML_CARD})
    try:
        save_oauth_state("st-bind", "hw_sso",
                         {"binding_user_id": 42, "redirect_uri": "https://cb/x"})
        _verified = SamlChannelStrategy(
            verify_assertion=lambda b64, card: {"uid": "77", "login": "bob"})
        with patch("src.kernel.auth_sdk.channel_strategy.get_channel_strategy",
                   return_value=_verified), \
             patch("src.domains.mgmt.routes.grape_auth._handle_oauth_bind") as hbind:
            saml_callback(_FakeRequest(cookies={}), "hw_sso",
                          SAMLResponse="b64", RelayState="st-bind")
        hbind.assert_called_once()
        args, _kw = hbind.call_args
        # (request, provider, external_id, user_info, access_token, refresh_token, binding_user_id)
        assert args[1] == "hw_sso" and args[2] == "77" and args[6] == 42
    finally:
        _clear_providers_cfg()


# --------------------------------------------- S48 共享校验 helper（GET 语义保留）

def test_s48_shared_helper_get_semantics_preserved():
    """_validate_channel_callback 直测：GET 的 state 无效+error → auth_failed。"""
    from src.domains.mgmt.routes.grape_auth import _validate_channel_callback

    req = _FakeRequest(cookies={})
    sd, popup, is_login, err = _validate_channel_callback(
        req, "st-none", "gitcode", error="access_denied")
    assert sd is None and err is not None
    assert "auth_failed" in str(err.headers.get("location", ""))

    sd, popup, is_login, err = _validate_channel_callback(req, "st-none", "gitcode")
    assert sd is None and err is not None
    assert "state_invalid" in str(err.headers.get("location", ""))
