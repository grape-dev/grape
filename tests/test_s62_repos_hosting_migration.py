"""D42（S62）用例：repos.py 托管主路径收口——工厂+适配器端口+卡配置。

用户拍板（领域纠偏）：36 处 `if provider == "github": ... else: <gitcode 存量>`
本身就是写法问题——正确形态=既有工厂（create_provider）+ 适配器端口 +
平台卡配置，与 D36-D41 同构。判据是"分派写法对不对"，不是"会不会踩"。

验收门（最硬的锁）：src/domains/mgmt/routes/repos.py 文件级 AST 守卫——
平台名字面量不得出现在任何比较节点（==/!=/in/元组包含）。
传 provider 值给统一入口（with_oauth_token(user, p, fn)）合法。

先测后改：本文件在迁移前为 RED（结构守卫报 36 处；gitee 行为用例
走 gitcode 折叠路径失败）；迁移后全绿。
"""
import ast
import inspect
import json
import uuid
from unittest.mock import patch, MagicMock

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
from src.kernel.token_resolver import GenericVcsProvider, GitCodeProvider, GitHubProvider, create_provider


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
    "bot_username": "g_grape_bot",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro) if hasattr(coro, "send") else coro


# ------------------------------------------------ S62 结构守卫（验收门）

def test_s62_structure_guard_repos_no_platform_compare():
    """repos.py 全文件：平台名字面量不得出现在比较节点（if/elif/in/元组）。"""
    src = open("src/domains/mgmt/routes/repos.py", encoding="utf-8").read()
    tree = ast.parse(src)
    banned = {"github", "gitcode", "gitee"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value in banned:
                    offenders.append(f"L{sub.lineno}:{sub.value}")
    assert not offenders, f"repos.py 仍有平台名比较 {len(offenders)} 处: {offenders[:12]}"


# ------------------------------------------------ S62 适配器端口契约

def test_s62_port_create_repo_generic_form(gitee_card):
    """generic 建仓：POST /user/repos + form（Gitee 系 v5 写接口要求）。"""
    prov = create_provider("gitee")
    with patch.object(GenericVcsProvider, "_http", return_value={"full_name": "u/r"}) as m:
        r = prov.create_repo("T", "repo-x", "desc", True)
    assert r["full_name"] == "u/r"
    args = m.call_args.args
    assert args[0] == "POST" and args[1] == "/user/repos" and args[2] == "T"
    assert m.call_args.kwargs.get("form") is True
    assert args[3]["name"] == "repo-x" and args[3]["private"] == "true"


def test_s62_port_create_repo_legacy_delegation():
    """存量委托：gitcode→repo_manager.create_repo；github→create_github_repo。"""
    with patch("src.domains.mgmt.user.repo_manager.create_repo", return_value={"n": 1}) as m_gc:
        r = create_provider("gitcode").create_repo("T", "r", "d", False)
        assert r == {"n": 1} and m_gc.call_args.kwargs["name"] == "r"
    with patch("src.domains.mgmt.user.repo_manager.create_github_repo", return_value={"n": 2}) as m_gh:
        r = create_provider("github").create_repo("T", "r", "d", True)
        assert r == {"n": 2} and m_gh.call_args.kwargs["private"] is True


def test_s62_port_find_existing_webhook_generic(gitee_card):
    prov = create_provider("gitee")
    with patch.object(GenericVcsProvider, "_http", return_value=[
        {"id": 5, "url": "http://other"}, {"id": 7, "url": "http://wh"}]):
        assert prov.find_existing_webhook("T", "o", "r", "http://wh") == 7
    with patch.object(GenericVcsProvider, "_http", return_value={"error": "x"}):
        assert prov.find_existing_webhook("T", "o", "r", "http://wh") is None


def test_s62_port_fetch_repos_with_role_generic(gitee_card):
    prov = create_provider("gitee")
    with patch.object(GenericVcsProvider, "_http", return_value=[
        {"full_name": "acme/web", "name": "web", "owner": {"login": "acme"},
         "permissions": {"admin": True, "push": True}, "html_url": "https://gitee.com/acme/web"}]):
        repos = prov.fetch_repos_with_role("T")
    assert repos[0]["full_name"] == "acme/web" and repos[0]["is_admin"] is True


def test_s62_port_legacy_fetch_delegation():
    with patch("src.domains.mgmt.user.repo_manager.fetch_repos_with_role", return_value=[{"a": 1}]) as m:
        assert create_provider("gitcode").fetch_repos_with_role("T") == [{"a": 1}]
        m.assert_called_once_with("T")
    with patch("src.domains.mgmt.user.repo_manager.fetch_github_repos_with_role", return_value=[{"b": 2}]) as m:
        assert create_provider("github").fetch_repos_with_role("T") == [{"b": 2}]
        m.assert_called_once_with("T")


def test_s62_port_get_collaborator_permission():
    """generic 返回 None（不确定不阻断）；存量委托原实现。"""
    assert create_provider("gitee").get_collaborator_permission("T", "o", "r", "u") is None
    with patch("src.domains.mgmt.user.repo_manager.get_member_permission", return_value={"access_level": 40}) as m:
        assert create_provider("gitcode").get_collaborator_permission("T", "o", "r", "u")["access_level"] == 40
    with patch("src.domains.mgmt.user.repo_manager.get_github_collaborator_permission", return_value="admin") as m:
        assert create_provider("github").get_collaborator_permission("T", "o", "r", "u") == "admin"


# ------------------------------------------------ S62 host_repo 行为

class _Body:
    def __init__(self, full_name):
        self.full_name = full_name


def _host_common_mocks():
    return [
        patch("src.api.session_store.ensure_default_agent_sessions"),
        patch("src.domains.mgmt.routes.repos.user_db"),
    ]


def test_s62_host_repo_gitcode_parity():
    """gitcode 托管：行为锁——setup_webhook(password=bot token)、列名/入参不变。"""
    import src.domains.mgmt.routes.repos as repos_mod
    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    from src.kernel.token_resolver import GitCodeProvider
    with patch.object(repos_mod, "get_user_token_for", return_value="utok"), \
            patch.object(repos_mod, "with_oauth_token", side_effect=lambda u, p, fn, *a, **k: fn("utok")), \
            patch("src.domains.mgmt.user.db.get_webhook_url", return_value="http://wh/webhook/gitcode"), \
            patch("src.domains.mgmt.user.db.get_global_bot_token_for", return_value="gbt"), \
            patch("src.domains.mgmt.user.repo_manager.resolve_bot_username", return_value="gb"), \
            patch.object(GitCodeProvider, "get_user_login", return_value="owner"), \
            patch("src.domains.mgmt.user.repo_manager.add_collaborator", return_value=(True, "")), \
            patch("src.domains.mgmt.user.repo_manager.accept_invite_for_repo", return_value=True), \
            patch("src.domains.mgmt.user.repo_manager.get_member_permission",
                  return_value={"access_level": 40}), \
            patch("src.domains.mgmt.user.repo_manager.find_existing_webhook", return_value=None), \
            patch("src.domains.mgmt.user.repo_manager.setup_webhook", return_value=77) as m_setup, \
            patch.object(repos_mod, "user_db") as m_udb, \
            patch("src.api.session_store.ensure_default_agent_sessions"), \
            patch("src.api.onboarding.cleanup_stale_repo_data"), \
            patch("src.domains.mgmt.user.db.set_host_status"):
        r = repos_mod.host_repo(_Body("owner/repo"), fake_user, provider="gitcode")
    assert "托管成功" in r["message"]
    m_setup.assert_called_once()
    # 路径 A parity：token=全局 bot token；password 空 → 存量函数内部回填
    # api_token（repo_manager.setup_webhook: 空 password 回落 token，API 层逐字节等价）
    assert m_setup.call_args.args[0] == "gbt"
    assert m_setup.call_args.kwargs.get("webhook_password") == ""
    kw = m_udb.add_managed_repo.call_args.kwargs
    assert kw.get("gitcode_webhook_token") == "gbt"
    assert kw.get("bot_gitcode_token") == "gbt"


def test_s62_host_repo_gitee_provider_ports(gitee_card):
    """gitee 托管（新路径，迁移前 RED）：走适配器端口 + 卡派生列。"""
    import src.domains.mgmt.routes.repos as repos_mod
    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    mock_prov = MagicMock()
    mock_prov.get_user_login.return_value = "owner"
    mock_prov.find_existing_webhook.return_value = None
    mock_prov.setup_webhook.return_value = 88
    mock_prov.add_collaborator.return_value = True
    mock_prov.accept_collaborator_invite.return_value = True
    mock_prov.get_collaborator_permission.return_value = {"access_level": 40}
    mock_prov.token_storage_field.return_value = "bot_gitee_token"
    mock_prov.webhook_token_field.return_value = "gitee_webhook_token"
    with patch.object(repos_mod, "get_user_token_for", return_value="gtok"), \
            patch.object(repos_mod, "with_oauth_token", side_effect=lambda u, p, fn, *a, **k: fn("gtok")), \
            patch("src.kernel.token_resolver.create_provider", return_value=mock_prov) as m_cp, \
            patch("src.domains.mgmt.user.db.get_webhook_url", return_value="http://wh/webhook/gitee"), \
            patch("src.domains.mgmt.user.db.get_global_bot_token_for", return_value="ggbt"), \
            patch("src.domains.mgmt.user.repo_manager.resolve_bot_username", return_value="ggb"), \
            patch.object(repos_mod, "user_db") as m_udb, \
            patch("src.api.session_store.ensure_default_agent_sessions"), \
            patch("src.api.onboarding.cleanup_stale_repo_data"), \
            patch("src.domains.mgmt.user.db.set_host_status"):
        r = repos_mod.host_repo(_Body("gitee:owner/repo"), fake_user, provider="gitee")
    assert "托管成功" in r["message"]
    m_cp.assert_called_with("gitee")
    mock_prov.setup_webhook.assert_called()
    kw = m_udb.add_managed_repo.call_args.kwargs
    assert kw.get("gitee_webhook_token") is not None  # 卡派生列（不再折叠 gitcode）
    assert "gitcode_webhook_token" not in kw


# ------------------------------------------------ S62 杂项收口行为

def test_s62_fetch_and_cache_gitee(gitee_card):
    """_fetch_and_cache：gitee 走 provider 端口 + gitee: 前缀（不再折叠）。"""
    import src.domains.mgmt.routes.repos as repos_mod
    mock_prov = MagicMock()
    mock_prov.fetch_repos_with_role.return_value = [
        {"full_name": "acme/web", "name": "web", "owner": "acme", "is_admin": True}]
    with patch("src.kernel.token_resolver.create_provider", return_value=mock_prov):
        repos = repos_mod._fetch_and_cache(1, "T", "gitee")
    assert repos[0]["repo_key"] == "gitee:acme/web"


def test_s62_fetch_and_cache_gitcode_parity():
    import src.domains.mgmt.routes.repos as repos_mod
    with patch("src.domains.mgmt.user.repo_manager.fetch_repos_with_role",
               return_value=[{"full_name": "acme/web", "name": "web", "owner": "acme"}]):
        repos = repos_mod._fetch_and_cache(1, "T", "gitcode")
    assert repos[0]["repo_key"] == "gitcode:acme/web"


def test_s62_repo_platform_token_gitee(gitee_card):
    """agent 会话换券：gitee 仓返回 gitee 用户 token（不再折叠 gitcode 取空 502）。"""
    import src.domains.mgmt.routes.repos as repos_mod
    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token", "agent_type": "chat"}
    with patch("src.api.deps.get_user_token_for", return_value="gt") as m_tok:
        tok = repos_mod._repo_platform_token("owner", "repo", fake_user, "gitee")
    assert tok == "gt"
    m_tok.assert_called_once()
    assert m_tok.call_args.kwargs.get("provider") == "gitee"


def test_s62_webhook_event_flags_gitee_v5(gitee_card):
    """S70（实弹发现）：gitee 建钩必须带 v5 布尔位事件集——原 generic 只传
    events 列表（GitHub 语义），gitee 全忽略 → issues/note 事件不投递。"""
    prov = create_provider("gitee")
    with patch.object(GenericVcsProvider, "_http", return_value={"id": 99}) as m:
        hid = prov.setup_webhook("T", "o", "r", "http://wh", webhook_secret="pw")
    assert hid == 99
    body = m.call_args[0][3]
    assert body.get("issues_events") is True, f"issues_events missing: {body}"
    assert body.get("note_events") is True, f"note_events missing: {body}"
    assert body.get("push_events") is True
    assert body.get("merge_requests_events") is True
    assert body.get("password") == "pw"


def test_s62_webhook_github_form_unchanged():
    """特征锁：GitHub 走存量 setup_github_webhook 委托（不被基座 v5 布尔位改动波及）。"""
    prov = create_provider("github")
    with patch("src.domains.mgmt.user.repo_manager.setup_github_webhook", return_value=11) as m:
        hid = prov.setup_webhook("T", "o", "r", "http://wh", webhook_secret="")
    assert hid == 11
    m.assert_called_once_with("T", "o", "r", "http://wh", "")
