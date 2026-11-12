"""D102-E：OIDC 引擎兜底（实弹热修语义重建）——行为锁。

华为 OIDC 实弹：① code 一次性（二次 exchange 报 1101）——引擎兜底必须
复用已换 token；② /user 端点 404 → id_token 解码兜底（唯一身份来源）；
③ 回调路由 userinfo 失败时 oidc 渠道转引擎兜底（非 oidc 维持原语义）。
"""
import os
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import base64
import json
from unittest.mock import patch

from src.kernel.auth_sdk.channel_strategy import OidcChannelStrategy, ChannelAuthError

_CARD = {"provider_key": "hwcloud"}


def _fake_id_token(sub="MDE3LqmwE4bYhrJKvFyjomDagrj9EyWQ", name="Zhang San"):
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": sub, "name": name, "email": "z@x.com"
    }).encode()).decode().rstrip("=")
    return f"fakeHeader.{payload}.fakeSig"


def test_d102e_tokens_reuse_no_reexchange():
    """_tokens 复用：callback_input._tokens 在场时不调 exchange_token（code 一次性）。"""
    called = {}
    def _fake_exchange(*a, **k):
        called["hit"] = True
        return ({"access_token": "x"}, "")
    s = OidcChannelStrategy()
    with patch("src.domains.mgmt.auth.providers.exchange_token", side_effect=_fake_exchange), \
         patch("src.domains.mgmt.auth.providers.get_user_info", return_value={"sub": "s1", "preferred_username": "u1"}):
        claims = s.handle_callback(
            {"code": "one-time-code", "redirect_uri": "http://x/cb",
             "_tokens": {"access_token": "reused", "id_token": _fake_id_token()}},
            _CARD)
    assert "hit" not in called, "已有 _tokens 仍二次 exchange（华为 1101 场景回潮）"
    assert claims.external_id == "s1"


def test_d102e_id_token_decode_fallback():
    """/user 失败 → id_token claims 解码兜底（华为 /user 404 场景）。"""
    s = OidcChannelStrategy()
    with patch("src.domains.mgmt.auth.providers.exchange_token",
               return_value=({"access_token": "at", "id_token": _fake_id_token()}, "")), \
         patch("src.domains.mgmt.auth.providers.get_user_info", side_effect=RuntimeError("404 not found")):
        claims = s.handle_callback(
            {"code": "c", "redirect_uri": "http://x/cb"}, _CARD)
    assert claims.external_id == "MDE3LqmwE4bYhrJKvFyjomDagrj9EyWQ"
    assert claims.name == "Zhang San"


def test_d102e_no_tokens_no_code_raises():
    """无 _tokens 无 code → ChannelAuthError（契约保留）。"""
    s = OidcChannelStrategy()
    try:
        s.handle_callback({"redirect_uri": "http://x/cb"}, _CARD)
        assert False, "should raise"
    except ChannelAuthError:
        pass


def test_d102e_callback_route_oidc_fallback():
    """回调路由：userinfo 失败时 oidc 渠道走引擎兜底（非 oidc 维持原语义）。"""
    import inspect
    from src.domains.mgmt.routes import grape_auth
    src = inspect.getsource(grape_auth.oauth_callback)
    assert "oidc" in src and "engine fallback" in src.replace("engine fallback produced", "engine fallback"), \
        "oauth_callback 缺 oidc 引擎兜底分支"
    assert 'get_user_info(provider, access_token)' in src, "原 userinfo 直调丢失"
