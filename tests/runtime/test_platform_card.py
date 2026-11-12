"""PlatformCard（仓库平台卡）Builder/校验/注册缓存用例——S18 校验面 + 默认卡回归。

对应设计 D1/D10/D14：双配置卡分离、构建即校验、三平台内置默认与现状一致。
"""
import pytest

from src.domains.runtime.vcs.platform_card import (
    PlatformCardError, build_platform_card, get_platform_card,
    list_vcs_platforms, invalidate_platform_card_cache,
)


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "hmac", "clone_auth": "basic_password",
    "bot_username": "my_grape_bot",
}


def test_builder_gitee_card_minimal_and_full():
    card = build_platform_card("gitee", GITEE_CARD)
    assert card.host == "gitee.com"
    assert card.api_auth == "query_token"
    assert card.webhook_verify == "hmac"
    assert card.token_env_name() == "BOT_GITEE_TOKEN"
    assert card.user_token_env_name() == "USER_GITEE_TOKEN"
    # 最小配置：默认派生 webhook_path/event_header/api_base
    minimal = build_platform_card("gitee", {"host": "gitee.com"})
    assert minimal.webhook_path == "/webhook/gitee"
    assert minimal.event_header == "X-Gitee-Event"
    assert minimal.api_base == "https://gitee.com"
    # 值对象不可变
    with pytest.raises(Exception):
        card.host = "x"  # type: ignore[misc]


@pytest.mark.parametrize("raw,err_fragment", [
    ({"dialect": "svn", "host": "h.com"}, "dialect"),
    ({"host": ""}, "host"),
    ({"host": "a/b"}, "host"),
    ({"host": "h.com", "webhook_path": "/hooks/x"}, "webhook_path"),
    ({"host": "h.com", "api_base": "ftp://x"}, "api_base"),
    ({"host": "h.com", "api_auth": "cookie"}, "api_auth"),
    ({"host": "h.com", "webhook_verify": "jwt"}, "webhook_verify"),
    ({"host": "h.com", "clone_auth": "ssh_key"}, "clone_auth"),
    ({"host": "h.com", "token_env": "bot-token"}, "token_env"),
])
def test_builder_rejects_invalid_cards(raw, err_fragment):
    with pytest.raises(PlatformCardError) as ei:
        build_platform_card("gitee", raw)
    assert err_fragment in str(ei.value)


def test_builder_rejects_bad_provider_name():
    with pytest.raises(PlatformCardError):
        build_platform_card("git hub", {"host": "h.com"})
    with pytest.raises(PlatformCardError):
        build_platform_card("", {"host": "h.com"})


def test_default_cards_match_current_behavior():
    """D14 存量回归锁基线：三平台内置默认卡与现状硬编码行为一致。"""
    invalidate_platform_card_cache()
    gc = get_platform_card("gitcode")
    gh = get_platform_card("github")
    gl = get_platform_card("gitlab")
    assert gc and gc.host == "gitcode.com"
    assert gc.api_base == "https://gitcode.com/api/v5"
    assert gc.api_auth == "query_token"        # GitCode v5：header 401 坑（D13 语义层）
    assert gc.webhook_verify == "hmac"
    assert gh and gh.host == "github.com" and gh.api_auth == "token_header"  # D22：存量 "token TK" 头形态
    assert gh.api_base == "https://api.github.com"
    assert gh.pat_create_url == "https://github.com/settings/tokens"          # D22：存储字段进卡
    assert gl and gl.dialect == "gitlab" and gl.webhook_verify == "token_compare"
    assert gl.clone_auth == "oauth2"
    assert gl.api_auth == "private_token"                                     # D22：存量 PRIVATE-TOKEN 头形态
    # D22：内置卡存储字段=存量 Provider 类现值（逐字节）
    assert gc.token_storage_field == "bot_gitcode_token"
    assert gc.webhook_token_field == "gitcode_webhook_token"
    assert gc.session_token_field == "repo_gitcode_token"
    assert gc.global_bot_status_meta_key == "global_gitcode_bot_token_status"
    assert gh.token_storage_field == "bot_github_token"
    assert gh.webhook_token_field == "github_webhook_token"
    assert gh.global_bot_status_meta_key == "global_github_bot_token_status"
    assert {"gitcode", "github", "gitlab"} <= set(list_vcs_platforms())


def test_card_has_no_oauth_fields():
    """D10：仓库平台卡不得携带 OAuth 字段（登录渠道卡专属）。"""
    card = build_platform_card("gitee", GITEE_CARD)
    forbidden = {"auth_url", "token_url", "user_url", "scope", "id_field",
                 "login_field", "whitelist_column"}
    assert not (forbidden & {f for f in dir(card) if not f.startswith("_")})
    with pytest.raises(Exception):
        # dataclass 冻结：无法塞 OAuth 字段
        card.auth_url = "x"  # type: ignore[attr-defined]
