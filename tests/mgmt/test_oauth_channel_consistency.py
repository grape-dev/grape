"""渠道卡一致性锁 + 登录/托管两类配置的分层守卫（fix/oauth-channel-consistency）。

反证用例（把对应修正改回旧形态即立红）：
1. Gitee 跳过授权页的判据是「请求 scope 与上次授权勾选范围一致」（Gitee 官方 OAuth
   文档原文）。旧卡写 "user_info emails"——emails 照抄 GitHub 的 user:email，不在
   Gitee 合法集内，scope 对不上就每次登录重新弹授权页。
2. Gitee / 华为 authorize 都不认 PKCE（参数只有 client_id/redirect_uri/response_type/
   scope/state），pkce_enabled 必须 false。
3. 华为原先只有管理面 client 凭据字段映射，没有渠道卡 → /api/auth/login/huawei 400。
4. 分层：登录渠道配置（configs/oauth-providers.json）与仓库托管配置
   （configs/platforms/*.json）字段互不渗透。
5. 凭据不进配置文件（唯一落点 = DB meta / 管理面）。
"""
import json

import pytest

from src.domains.mgmt.auth.providers import (
    _AUTH_CHANNELS_FILE, _builtin_channels_from_file, builtin_channels,
)
from src.kernel.auth_sdk.channel_strategy import ChannelAuthError, OAuth2ChannelStrategy


# Gitee 官方 OAuth 文档列出的合法 scope 集（无 emails）
GITEE_VALID_SCOPES = {
    "user_info", "projects", "pull_requests", "issues", "notes",
    "keys", "hook", "groups", "gists", "enterprises",
}

# 托管（仓库）侧能力位——绝不允许出现在登录渠道卡里
VCS_ONLY_FIELDS = {
    "dialect", "host", "webhook_path", "event_header", "api_base", "api_auth",
    "webhook_verify", "webhook_dialect", "clone_auth", "issue_write_form",
    "branch_create_form", "pr_head_filter", "pr_comment_endpoint",
    "pr_labels_via_issues", "git_tree_family", "token_storage_field",
    "webhook_token_field", "session_token_field",
    "global_bot_meta_key", "global_bot_status_meta_key", "download_base",
    "user_images_base", "bot_username", "token_env",
    # 注：pat_create_url 是"双面字段"（托管卡声明 PAT 创建页；登录卡声明绑定弹窗的
    # 直达链接，缺失时回落托管卡），故不在本集合内。emails_endpoint 只属登录侧。
}
# 登录（OAuth）侧能力位——绝不允许出现在仓库平台卡里
AUTH_ONLY_FIELDS = {
    "auth_url", "token_url", "user_url", "scope", "pkce_enabled",
    "accept_header", "id_field", "login_field", "name_field", "email_field",
    "avatar_field", "claims_map", "idp_sso_url", "attribute_map",
    "emails_endpoint", "channel_type",
}


def test_gitee_channel_scope_within_official_set():
    """scope 必须落在 Gitee 合法集内，否则与上次授权范围对不上 → 每次都弹授权页。"""
    scopes = set(str(builtin_channels()["gitee"].get("scope", "")).split())
    assert scopes, "gitee 渠道卡必须声明 scope（Gitee 用 scope 一致性判断是否跳过授权页）"
    invalid = scopes - GITEE_VALID_SCOPES
    assert not invalid, f"gitee scope 含 Gitee 未支持的值: {sorted(invalid)}"


def test_gitee_and_huawei_disable_pkce():
    cards = builtin_channels()
    assert cards["gitee"]["pkce_enabled"] is False
    assert cards["huawei"]["pkce_enabled"] is False


def test_huawei_channel_card_exists_and_passes_builder():
    card = builtin_channels().get("huawei")
    assert card is not None, "华为必须有渠道卡，否则管理面填了 client_id 也建不出登录渠道"
    assert "{access_token}" in card["user_url"], "华为 GOpen.User.getInfo 只认查询参数形式的 access_token"
    assert card["id_field"] == "openID" and card["login_field"] == "openID", "华为不返回登录名，身份用 openID"
    assert card["avatar_field"] == "headPictureURL"
    OAuth2ChannelStrategy().validate_card({**card, "provider_key": "huawei"})


def test_channel_card_builder_rejects_missing_endpoint():
    with pytest.raises(ChannelAuthError):
        OAuth2ChannelStrategy().validate_card({
            "provider_key": "huawei", "auth_url": "", "token_url": "https://x/token",
            "user_url": "https://x/user"})


# ---------------------------------------------------------------- 分层守卫（核心）

def test_auth_channel_cards_hold_no_vcs_fields():
    """登录渠道卡不得出现托管能力位——两类配置必须物理分离、字段互不渗透。"""
    offenders = {p: sorted(set(c) & VCS_ONLY_FIELDS) for p, c in builtin_channels().items()}
    offenders = {p: f for p, f in offenders.items() if f}
    assert not offenders, f"登录渠道卡混入托管字段（应放 configs/platforms/*.json）: {offenders}"


def test_vcs_platform_cards_hold_no_auth_fields():
    """仓库平台卡不得出现 OAuth 字段——否则又变成一份配置管两件事。"""
    import dataclasses as _dc
    from src.domains.runtime.vcs.platform_card import get_platform_card, list_vcs_platforms
    offenders = {}
    for provider in list_vcs_platforms():
        card = get_platform_card(provider)
        if card is None:
            continue
        hit = sorted({f.name for f in _dc.fields(card)} & AUTH_ONLY_FIELDS)
        if hit:
            offenders[provider] = hit
    assert not offenders, f"仓库平台卡混入 OAuth 字段（应放 configs/oauth-providers.json）: {offenders}"


def test_shipped_auth_config_file_has_no_credentials():
    """随仓库发布的渠道配置不得含凭据（凭据只进 DB meta / 管理面）。"""
    with open(_AUTH_CHANNELS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    leaked = {p: [k for k in ("client_id", "client_secret") if k in card]
              for p, card in data.items()}
    leaked = {p: k for p, k in leaked.items() if k}
    assert not leaked, f"渠道配置文件出现凭据字段: {leaked}"


def test_loader_strips_credentials_from_channel_file(tmp_path):
    """有人把凭据写进文件时，加载器剥离——绝不把凭据当协议参数加载。"""
    p = tmp_path / "oauth-providers.json"
    p.write_text(json.dumps({
        "demo": {"name": "Demo", "auth_url": "https://d/a", "token_url": "https://d/t",
                 "user_url": "https://d/u", "client_id": "leak", "client_secret": "leak2"}
    }), encoding="utf-8")
    card = _builtin_channels_from_file(str(p))["demo"]
    assert "client_id" not in card and "client_secret" not in card


def test_loader_rejects_unknown_field(tmp_path):
    """未知键整卡拒绝（拼错键 = 能力静默丢失）。"""
    p = tmp_path / "oauth-providers.json"
    p.write_text(json.dumps({
        "demo": {"name": "Demo", "auth_url": "https://d/a", "token_url": "https://d/t",
                 "user_url": "https://d/u", "host": "d.com"}
    }), encoding="utf-8")
    assert _builtin_channels_from_file(str(p)) == {}


def test_doc_keys_are_ignored(tmp_path):
    """下划线键=文档键：_readme 不被当成渠道名，卡内 _note 被剥离（example 可以写满说明）。"""
    p = tmp_path / "oauth-providers.json"
    p.write_text(json.dumps({
        "_readme": ["说明一", "说明二"],
        "demo": {"_note": "这是渠道说明", "name": "Demo", "auth_url": "https://d/a",
                 "token_url": "https://d/t", "user_url": "https://d/u"}
    }), encoding="utf-8")
    cards = _builtin_channels_from_file(str(p))
    assert list(cards) == ["demo"]
    assert "_note" not in cards["demo"]


def test_shipped_example_file_is_loadable():
    """随仓库发布的 example 必须能直接复制成真文件加载（说明键不阻断、卡不缺端点）。"""
    import os as _os
    example = _os.path.join(_os.path.dirname(_AUTH_CHANNELS_FILE), "oauth-providers.example.json")
    cards = _builtin_channels_from_file(example)
    assert set(cards) >= {"gitcode", "github", "gitee", "huawei"}


def test_platform_card_files_ignore_doc_keys():
    """平台卡文件里的 _readme/_fields 说明键必须被剥离——否则会触发"未知键整卡拒绝"。"""
    from src.domains.runtime.vcs.platform_card import _CONFIGS_DIR, _builtin_raws_from_dir
    raws = _builtin_raws_from_dir(_CONFIGS_DIR)
    assert set(raws) >= {"gitcode", "github", "gitee", "gitlab"}, raws.keys()
    for provider, raw in raws.items():
        leftovers = [k for k in raw if str(k).startswith("_")]
        assert not leftovers, f"{provider} 说明键未被剥离: {leftovers}"
        assert "auth_url" not in raw, f"{provider} 平台卡混入 OAuth 端点"