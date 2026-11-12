"""D26（S38-S40/S42）用例：协议引擎契约、注册表、身份声明归一。"""
import json

import pytest

from src.kernel.auth_sdk.channel_strategy import (
    IdentityClaims, ChannelAuthError, LoginChannelStrategy,
    OAuth2ChannelStrategy, SamlChannelStrategy,
    register_channel_strategy, build_channel_strategy, get_channel_strategy,
    normalize_identity_claims,
)
from src.kernel.session_manager import db, db_lock


# ---------------------------------------------------------------- S40 注册表

def test_s40_builtin_oauth2_registered():
    assert get_channel_strategy("oauth2") is not None
    assert isinstance(get_channel_strategy("oauth2"), OAuth2ChannelStrategy)


def test_s40_register_custom_type_and_use():
    class OidcEngine(LoginChannelStrategy):
        def initiate_auth(self, state, redirect_uri, card):
            return {"redirect_url": f"{card['issuer']}/authorize?state={state}"}

        def handle_callback(self, callback_input, card):
            return normalize_identity_claims(
                card.get("provider_key", ""),
                {"sub": "u1", "preferred_username": "alice"},
                {"id_field": "sub", "login_field": "preferred_username"})

    register_channel_strategy("oidc_test", OidcEngine())
    eng = get_channel_strategy("oidc_test")
    claims = eng.handle_callback({}, {"provider_key": "corp", "issuer": "https://idp"})
    assert claims.external_id == "u1" and claims.login == "alice"
    # 引擎实例化零平台代码：差异全在卡
    assert eng.initiate_auth("st", "", {"issuer": "https://idp"})["redirect_url"] == \
        "https://idp/authorize?state=st"


def test_s40_builtin_cannot_be_overridden():
    with pytest.raises(ChannelAuthError):
        register_channel_strategy("oauth2", SamlChannelStrategy())


def test_s40_unregistered_type_rejected():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (json.dumps({"weird": {"type": "cas", "name": "CAS"}}),))
        db.commit()
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    invalidate_provider_cache()
    try:
        with pytest.raises(ChannelAuthError) as ei:
            build_channel_strategy("weird")
        assert "not registered" in str(ei.value)   # 明确报错，不静默回退 oauth2
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
            db.commit()
        invalidate_provider_cache()


# ---------------------------------------------------------------- S39 契约

def test_s39_oauth2_initiate_delegates():
    from unittest.mock import patch
    with patch("src.domains.mgmt.auth.providers.build_auth_url", return_value={"redirect_url": "https://gitcode.com/oauth/authorize?x=1"}) as ba:
        out = OAuth2ChannelStrategy().initiate_auth("st", "https://cb", {"provider_key": "gitcode"})
    assert out == {"redirect_url": "https://gitcode.com/oauth/authorize?x=1"}
    ba.assert_called_once()


def test_s39_contract_shape():
    """契约形态：initiate 产 redirect/form；callback 产 IdentityClaims；
    refresh 未实现协议明确报错（非静默）。"""
    saml = SamlChannelStrategy(verify_assertion=lambda b, c: {"external_id": "77", "login": "bob"})
    out = saml.initiate_auth("st", "", {"idp_sso_url": "https://idp/sso"})
    assert "redirect_url" in out and "RelayState=st" in out["redirect_url"]
    claims = saml.handle_callback({"SAMLResponse": "b64"}, {})
    assert isinstance(claims, IdentityClaims) and claims.login == "bob"
    with pytest.raises(ChannelAuthError):
        OAuth2ChannelStrategy().refresh({}, {})   # 契约明确：无刷新语义即报错


# ---------------------------------------------------------------- S42 归一化

def test_s42_external_id_required_name_optional():
    with pytest.raises(ChannelAuthError):
        normalize_identity_claims("p", {"login": "a"}, {})   # 主键缺失拒绝
    c = normalize_identity_claims("p", {"id": "9", "login": "a"}, {})  # name 缺失降级
    assert c.name == "" and c.email == "" and c.external_id == "9"
    # login 缺失 → external_id 兜底（白名单键降级）
    c2 = normalize_identity_claims("p", {"id": "9"}, {})
    assert c2.login == "9"


def test_s42_saml_multivalue_attributes():
    c = normalize_identity_claims(
        "p", {"external_id": ["7"], "login": ["bob"], "name": ["Bob B"]},
        {"id_field": "external_id", "login_field": "login", "name_field": "name"})
    assert c.external_id == "7" and c.login == "bob" and c.name == "Bob B"


# ---------------------------------------------------------------- S38 骨架安全默认

def test_s38_saml_skeleton_safety_defaults():
    saml = SamlChannelStrategy()   # 未注入验签（等真实 IdP 元数据，S41/S38 实弹）
    with pytest.raises(ChannelAuthError):
        saml.handle_callback({"SAMLResponse": "b64"}, {})  # 未验签断言拒绝登录
    with pytest.raises(ChannelAuthError):
        saml.initiate_auth("st", "", {})                   # 卡缺 idp_sso_url 拒绝


# ---------------------------------------------------------------- S44 OIDC 引擎

def test_s44_oidc_engine_builtin_and_config_driven():
    from src.kernel.auth_sdk.channel_strategy import OidcChannelStrategy, build_channel_strategy
    assert isinstance(get_channel_strategy("oidc"), OidcChannelStrategy)
    # 卡字段契约：缺端点构建时拒绝（S45 顺带覆盖 oidc）
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (json.dumps({"corp_oidc": {"type": "oidc", "name": "Corp"}}),))
        db.commit()
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    invalidate_provider_cache()
    try:
        with pytest.raises(ChannelAuthError) as ei:
            build_channel_strategy("corp_oidc")
        assert "missing fields" in str(ei.value)
        # 补全端点 → 构建成功，claims 映射纯卡
        with db_lock:
            db.cursor().execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
                (json.dumps({"corp_oidc": {"type": "oidc", "name": "Corp",
                                            "auth_url": "https://idp/authorize",
                                            "token_url": "https://idp/token",
                                            "user_url": "https://idp/userinfo"}}),))
            db.commit()
        invalidate_provider_cache()
        eng = build_channel_strategy("corp_oidc")
        assert isinstance(eng, OidcChannelStrategy)
        # 标准 claims 归一（mock 换 token + userinfo）
        from unittest.mock import patch
        with patch("src.domains.mgmt.auth.providers.exchange_token",
                   return_value=({"access_token": "at"}, "")), \
             patch("src.domains.mgmt.auth.providers.get_user_info",
                   return_value={"sub": "s1", "preferred_username": "alice",
                                 "name": "Alice", "email": "a@x.com"}):
            claims = eng.handle_callback({"code": "c", "redirect_uri": "https://cb"}, {})
        assert (claims.external_id, claims.login, claims.name, claims.email) == \
            ("s1", "alice", "Alice", "a@x.com")
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
            db.commit()
        invalidate_provider_cache()


# ---------------------------------------------------------------- S45 卡字段契约

def test_s45_oauth2_builtin_cards_pass_validation():
    """存量渠道卡（gitcode/github 默认卡）字段齐全，构建不拒绝（回归）。"""
    eng = build_channel_strategy("gitcode")
    assert isinstance(eng, OAuth2ChannelStrategy)
    eng2 = build_channel_strategy("github")
    assert isinstance(eng2, OAuth2ChannelStrategy)


def test_s45_saml_card_missing_fields_rejected():
    from src.kernel.session_manager import db as _db, db_lock as _dl
    with _dl:
        _db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (json.dumps({"hw_sso": {"type": "saml", "name": "HW SSO"}}),))
        _db.commit()
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    invalidate_provider_cache()
    try:
        with pytest.raises(ChannelAuthError) as ei:
            build_channel_strategy("hw_sso")
        msg = str(ei.value)
        assert "idp_sso_url" in msg and "attribute_map.external_id" in msg
    finally:
        with _dl:
            _db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
            _db.commit()
        invalidate_provider_cache()


# ---------------------------------------------------------------- S46 身份键稳定性

def test_s46_saml_email_attribute_as_external_id_rejected():
    """SAML 卡把 email 类属性配成 external_id → 构建时拒绝并报属性名。"""
    from src.kernel.session_manager import db as _db, db_lock as _dl
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    with _dl:
        _db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (json.dumps({"hw_sso": {"type": "saml", "name": "HW SSO",
                                    "idp_sso_url": "https://idp/sso",
                                    "attribute_map": {"external_id": "emailAddress",
                                                      "login": "login"}}}),))
        _db.commit()
    invalidate_provider_cache()
    try:
        with pytest.raises(ChannelAuthError) as ei:
            build_channel_strategy("hw_sso")
        msg = str(ei.value)
        assert "emailAddress" in msg and "S46" in msg
        # transient NameID 同族拒绝
        with _dl:
            _db.cursor().execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
                (json.dumps({"hw_sso": {"type": "saml", "name": "HW SSO",
                                        "idp_sso_url": "https://idp/sso",
                                        "attribute_map": {"external_id": "transientId"}}}),))
            _db.commit()
        invalidate_provider_cache()
        with pytest.raises(ChannelAuthError):
            build_channel_strategy("hw_sso")
    finally:
        with _dl:
            _db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
            _db.commit()
        invalidate_provider_cache()


def test_s46_email_shaped_external_id_value_rejected():
    """回调归一时 external_id 值为 email 形态 → 拒绝（协议无关兜底）。"""
    with pytest.raises(ChannelAuthError) as ei:
        normalize_identity_claims("p", {"id": "u@corp.com", "login": "u"}, {})
    assert "S46" in str(ei.value)
    # 稳定数值 ID 不受影响（回归）
    c = normalize_identity_claims("p", {"id": "12345", "login": "u"}, {})
    assert c.external_id == "12345"


# ---------------------------------------------------------------- S47 复合键不变量

def test_s47_composite_key_two_providers_same_external_id():
    """UNIQUE(provider, external_id) 复合唯一：两 IdP 同 sub 互不冲突。"""
    a = normalize_identity_claims("idp_a", {"id": "s1", "login": "alice"}, {})
    b = normalize_identity_claims("idp_b", {"id": "s1", "login": "alice"}, {})
    assert a.external_id == b.external_id == "s1"
    assert a.provider != b.provider  # (provider, external_id) 两组不同键，各自绑定


def test_s47_ddl_composite_unique_pinned():
    """结构性断言：external_identities DDL 的唯一约束必须是复合键
    （防未来退化成全局唯一 → 跨 IdP sub 撞键误拒/误绑）。"""
    import re as _re
    src = open("src/api/db_setup.py", encoding="utf-8").read()
    # 剥注释/空行后再断言，防注释里的字样误命中
    stripped = "\n".join(
        ln.split("--")[0] for ln in src.splitlines())
    assert _re.search(
        r"UNIQUE\s*\(\s*provider\s*,\s*external_id\s*\)", stripped), \
        "external_identities unique constraint must stay (provider, external_id)"
