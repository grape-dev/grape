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


def test_gitlab_pat_create_url_uses_current_gitlab_route():
    """GitLab 17.0 删了 /profile/personal_access_tokens。PAT 深链必须是
    /-/user_settings/personal_access_tokens，自建实例同构派生（一实例一卡）。"""
    invalidate_platform_card_cache()
    gl = get_platform_card("gitlab")
    assert gl is not None and gl.pat_create_url == "https://gitlab.com/-/user_settings/personal_access_tokens"

    selfhosted = build_platform_card("gitlab-corp", {"dialect": "gitlab", "host": "gl.example.com"})
    assert selfhosted.pat_create_url == "https://gl.example.com/-/user_settings/personal_access_tokens"

    # 其他方言保持原路径（gitcode/gitee 的 PAT 页仍在 /profile/...）
    gc = get_platform_card("gitcode")
    assert gc is not None and gc.pat_create_url.endswith("/profile/personal_access_tokens")


def test_card_has_no_oauth_fields():
    """D10：仓库平台卡不得携带 OAuth 字段（登录渠道卡专属）。"""
    card = build_platform_card("gitee", GITEE_CARD)
    forbidden = {"auth_url", "token_url", "user_url", "scope", "id_field",
                 "login_field", "whitelist_column"}
    assert not (forbidden & {f for f in dir(card) if not f.startswith("_")})
    with pytest.raises(Exception):
        # dataclass 冻结：无法塞 OAuth 字段
        card.auth_url = "x"  # type: ignore[attr-defined]


def test_apply_card_overrides_wins_over_file_and_table():
    """A2：管理面覆盖层必须是最后叠加的一层（文件 → meta → 表行 → 覆盖层）。"""
    from src.domains.runtime.vcs.platform_card import apply_card_overrides

    base = {"gitlab": {"name": "GitLab", "dialect": "gitlab", "host": "gitlab.com",
                       "ca_bundle": "", "download_base": "https://gitlab.com"}}
    apply_card_overrides(base, {"gitlab": {"ca_bundle": "/etc/pki/tls/cert.pem"},
                                "nope": {"host": "x.example"}})
    assert base["gitlab"]["ca_bundle"] == "/etc/pki/tls/cert.pem"
    assert base["gitlab"]["host"] == "gitlab.com"       # 只覆盖声明过的键
    assert "nope" not in base                           # 卡不存在的 provider 不凭空造


def test_card_field_overrides_only_reports_changed_keys():
    """A2：只记相对生效卡真正变化的字段；改回文件值 → 自动不产生覆盖。"""
    import dataclasses as _dc

    from src.domains.runtime.vcs.platform_card import card_field_overrides

    card = build_platform_card("gitlab", {"host": "gitlab.com"})
    raw = _dc.asdict(build_platform_card("gitlab", {"host": "gitlab.com"}))
    raw.pop("provider", None)
    raw["event_map"] = raw.pop("event_map_overrides", {})
    raw["fields"] = raw.pop("fields_overrides", {})
    assert card_field_overrides(raw, card) == {}

    changed = dict(raw, ca_bundle="/etc/pki/tls/cert.pem")
    assert card_field_overrides(changed, card) == {"ca_bundle": "/etc/pki/tls/cert.pem"}

    # 显式置空也算改动（键存在即尊重）：文件给过值，管理面清空 → 覆盖落 ""。
    card2 = build_platform_card("gitlab", {"host": "gitlab.com",
                                           "download_base": "https://gitlab.com"})
    cleared = dict(raw, download_base="")
    assert card_field_overrides(cleared, card2) == {"download_base": ""}


def test_row_to_card_raw_drops_empty_columns():
    """表行 → raw 不能把 NULL 的 "" 照抄进去：可选枚举列会被 Builder 整卡拒绝。"""
    from src.domains.runtime.vcs.platform_card import row_to_card_raw

    row = {
        "id": "x", "enabled": 1, "source": "config", "provider": "gitlab",
        "name": "GitLab", "dialect": "gitlab", "host": "gitlab.com",
        "api_base": "https://gitlab.com/api/v4", "api_auth": "private_token",
        "webhook_path": "/webhook/gitlab", "event_header": "X-Gitlab-Event",
        "webhook_verify": "token_compare", "webhook_dialect": "gitlab_v4",
        "clone_auth": "oauth2", "branch_create_form": "", "issue_write_form": "",
        "issue_search": 0, "ca_bundle": "/etc/pki/tls/cert.pem",
    }
    raw = row_to_card_raw(row)
    assert "branch_create_form" not in raw          # 空串 = 未声明
    assert "issue_write_form" not in raw
    assert raw["issue_search"] == 0                 # 0 是有效值，不能被当空值丢掉
    assert "id" not in raw and "enabled" not in raw  # 非卡定义列
    card = build_platform_card("gitlab", raw)       # 以前这里会抛 PlatformCardError
    assert card.ca_bundle == "/etc/pki/tls/cert.pem"


def test_card_overrides_value_object_contract():
    """CardOverrides：只留变化键、改回基线即撤销、坏 JSON 不拖垮读卡。"""
    from src.domains.runtime.vcs.card_overrides import CardOverrides, diff_card_fields

    base = build_platform_card("gitlab", {"host": "gitlab.com"})
    ov = CardOverrides.empty().with_provider("gitlab", {"ca_bundle": "/opt/ca.pem"})
    assert ov.for_provider("gitlab") == {"ca_bundle": "/opt/ca.pem"}
    assert ov.to_json()

    raws = {"gitlab": {"host": "gitlab.com", "ca_bundle": ""}}
    ov.apply_to(raws)
    assert raws["gitlab"]["ca_bundle"] == "/opt/ca.pem"

    # 撤销：同名 provider 传空字段
    assert CardOverrides.from_json(ov.to_json()).with_provider("gitlab", {}).providers() == []

    # 反向：提交与基线一致 → 无覆盖
    import dataclasses as _dc
    raw = _dc.asdict(base); raw.pop("provider", None)
    raw["event_map"] = raw.pop("event_map_overrides", {})
    raw["fields"] = raw.pop("fields_overrides", {})
    assert diff_card_fields(raw, base) == {}

    # 坏 JSON / 非对象 → 视为空覆盖
    assert CardOverrides.from_json("{not json").providers() == []
