"""托管配置：webhook 路径与 bot 协作者身份必须解耦（Issue #372 遗留耦合）。

背景：gitlab_v4 出于安全把 `bot_token` 置空（PAT 不能当 webhook token，否则随
X-Gitlab-Token 外泄给项目维护者）。但同一个键同时被用来决定「是否邀请 bot 协作者」
和「仓库 bot 凭据」，于是 GitLab 仓库既不邀 bot，回帖也只能用用户身份。
修法：新增 `collab_bot_token`（恒为全局 bot token），webhook 路径判定仍看 `bot_token`。
"""
import pytest

from src.domains.mgmt.user import repo_manager as rm


@pytest.fixture()
def stub_cards(monkeypatch):
    class _Card:
        def __init__(self, provider, dialect):
            self.provider = provider
            self.dialect = dialect
            self.api_base = f"https://{provider}.example.com/api"
            self.webhook_dialect = dialect
            self.webhook_token_field = "webhook"
            self.token_storage_field = "bot"

    cards = {"gitlab": _Card("gitlab", "gitlab_v4"), "gitcode": _Card("gitcode", "gitee_v5")}

    # 这两个是函数体内 import 的，必须打在来源模块上
    import src.domains.runtime.public as rt_public
    import src.domains.mgmt.user.db as user_db

    monkeypatch.setattr(rt_public, "get_platform_card", lambda p: cards[p], raising=False)
    monkeypatch.setattr(user_db, "get_global_bot_token_for", lambda p: f"bot-token-{p}",
                        raising=False)
    monkeypatch.setattr(rm, "resolve_bot_username", lambda p: f"{p}-bot", raising=False)
    monkeypatch.setattr(rm, "_webhook_path_from_card",
                        lambda p, d: f"/webhook/{p}", raising=False)
    return cards


def test_gitlab_keeps_webhook_path_b_but_keeps_bot_identity(stub_cards):
    cfg = rm.get_hosting_provider_config("gitlab")
    assert cfg["webhook_path_b"] is True, "gitlab_v4 的 webhook 必须走路径 B（随机密钥）"
    assert cfg["bot_token"] == "bot-token-gitlab", \
        "bot_token 仍是全局 bot token：协作者邀请 + 回帖凭据（本次修复点）"
    assert cfg["bot_username"] == "gitlab-bot"


def test_other_platforms_keep_webhook_path_a(stub_cards):
    cfg = rm.get_hosting_provider_config("gitcode")
    assert cfg["webhook_path_b"] is False
    assert cfg["bot_token"] == "bot-token-gitcode"

def test_db_fields_with_global_bot(monkeypatch):
    """场景B：有全局 bot → 落 token + username（回帖走 bot 身份）。"""
    import src.domains.mgmt.user.db as _udb
    from src.domains.mgmt.routes.repos import _bot_db_fields

    monkeypatch.setattr(_udb, "get_global_bot_token_for", lambda p: "bot-token")
    monkeypatch.setattr(rm, "resolve_bot_username", lambda p: "grape-bot")
    cfg = rm.get_hosting_provider_config("gitlab")
    tok, name = _bot_db_fields(cfg)
    assert (tok, name) == ("bot-token", "grape-bot")


def test_db_fields_with_user_pat_only(monkeypatch):
    """场景A：没有全局 bot（只有用户 PAT）→ token/username 都留空，走用户身份。"""
    import src.domains.mgmt.user.db as _udb
    from src.domains.mgmt.routes.repos import _bot_db_fields

    monkeypatch.setattr(_udb, "get_global_bot_token_for", lambda p: "")
    monkeypatch.setattr(rm, "resolve_bot_username", lambda p: "grape-bot")
    cfg = rm.get_hosting_provider_config("gitlab")
    assert cfg["webhook_path_b"] is True            # webhook 仍走随机密钥
    tok, name = _bot_db_fields(cfg)
    assert (tok, name) == ("", ""), "无 bot token 时不得只写 username（会堵死用户身份回帖）"


def test_bot_token_not_stored_when_invite_fell_back(monkeypatch):
    """场景C：有全局 bot 但（如项目机器人跨项目被拒）邀请失败回落路径 B →
    不得再落 bot token，否则推送会拿一个没有该仓权限的 token 去推。"""
    import src.domains.mgmt.user.db as _udb
    from src.domains.mgmt.routes.repos import _bot_db_fields

    monkeypatch.setattr(_udb, "get_global_bot_token_for", lambda p: "project-bot-token")
    monkeypatch.setattr(rm, "resolve_bot_username", lambda p: "project_bot")
    cfg = rm.get_hosting_provider_config("gitlab")
    assert _bot_db_fields(cfg, usable=False) == ("", "")
