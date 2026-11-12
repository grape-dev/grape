"""D30（S50）用例：外围第二批卡化——admin 面与路由解析点。

全部单元测试：meta 卡 + mock 委托点 / 传输层，不触碰真实平台。
覆盖：_provider_from_hint 候选集、_get_user_provider_and_token 平台顺序、
config_store.verify_token 卡驱动、resolve_bot_username 来源链。
"""
import json
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


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


def _insert_managed_repo(repo_key: str):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos "
            "(repo_key, repo_owner, repo_name, full_name, host_user_id) "
            "VALUES (?, 'acme', 'web', 'acme/web', 0)",
            (repo_key,))
        db.commit()


def _delete_managed_repo(repo_key: str):
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.commit()


# ------------------------------------------------------- S50 provider 解析

def test_s50_provider_from_hint_gitee_registered(gitee_card):
    """gitee 卡就位 + managed_repos 有 gitee 行 → 裸名解析出 gitee（双前缀硬编码消除）。"""
    from src.domains.mgmt.routes.repos import _provider_from_hint
    _insert_managed_repo("gitee:acme/web")
    try:
        assert _provider_from_hint("acme/web") == "gitee"
        assert _provider_from_hint("gitee:acme/web") == "gitee"
    finally:
        _delete_managed_repo("gitee:acme/web")


def test_s50_provider_from_hint_legacy_regression():
    """无 gitee 卡（默认三平台）：前缀/双候选查询行为与旧版一致。"""
    from src.domains.mgmt.routes.repos import _provider_from_hint
    _insert_managed_repo("github:octo/app")
    try:
        assert _provider_from_hint("github:octo/app") == "github"
        assert _provider_from_hint("octo/app") == "github"
        assert _provider_from_hint("gitcode:octo/app") == "gitcode"
    finally:
        _delete_managed_repo("github:octo/app")
    assert _provider_from_hint("no/such-repo-anywhere") == ""
    assert _provider_from_hint("") == ""


def test_s50_get_user_token_platform_order(gitee_card):
    """login_provider 第 5 平台优先；未配卡平台不进优先序（防误判）。"""
    from src.domains.mgmt.routes.repos import _get_user_provider_and_token
    _toks = {"gitee": "tok-gitee", "github": "tok-github", "gitcode": "tok-gitcode"}
    with patch("src.domains.mgmt.routes.repos.get_user_token_for",
               side_effect=lambda u, user_id=0, provider="gitcode": _toks[provider]):
        # gitee 已配卡且 login_provider=gitee → gitee 优先
        assert _get_user_provider_and_token("u", "gitee", 7) == ("gitee", "tok-gitee")
        # 无 login 偏好 → github 优先（存量顺序保持）
        assert _get_user_provider_and_token("u", "", 7) == ("github", "tok-github")
        # login_provider 非法/未配 → 跳过进 github
        assert _get_user_provider_and_token("u", "nope", 7) == ("github", "tok-github")


def test_s50_get_user_token_no_platform_token():
    """全平台无 token → ("", "")（降级语义保持）。"""
    from src.domains.mgmt.routes.repos import _get_user_provider_and_token
    with patch("src.domains.mgmt.routes.repos.get_user_token_for", return_value=""):
        assert _get_user_provider_and_token("u", "gitcode", 7) == ("", "")


def test_s50_provider_from_token_delegates():
    from src.domains.mgmt.routes.repos import _provider_from_token
    with patch("src.domains.mgmt.routes.repos.get_user_token_for", return_value="tok-g"):
        assert _provider_from_token({"username": "u", "id": 7}) == ("github", "tok-g")


# ------------------------------------------------------- S50 admin 面

def test_s50_config_store_verify_token_card_driven(gitee_card):
    """config_store.verify_token 走 create_provider（卡驱动 /user），返回形态保持。"""
    from src.domains.mgmt.admin import config_store

    class _P:
        def verify_token(self, token):
            assert token == "tok-x"
            return {"login": "bob", "name": "Bob B"}

    with patch("src.kernel.token_resolver.create_provider", return_value=_P()):
        out = config_store.verify_token("gitee", "tok-x")
    assert out == {"provider": "gitee", "login": "bob", "name": "Bob B"}


def test_s50_config_store_verify_token_gitee_real_card(gitee_card):
    """真实卡路径：gitee /user 命中卡 api_base + query_token（传输层 mock）。"""
    from src.domains.mgmt.admin import config_store
    seen = {}

    def _fake_http(self, method, path, token, data=None):
        seen["url"] = f"{self._card.api_base}{path}"
        seen["token"] = token
        seen["auth"] = self._card.api_auth
        return {"login": "bob", "name": "Bob"}

    with patch("src.kernel.token_resolver.GenericVcsProvider._http", _fake_http):
        out = config_store.verify_token("gitee", "tok-x")
    assert out["provider"] == "gitee" and out["login"] == "bob"
    assert seen["url"] == "https://gitee.com/api/v5/user"
    assert seen["token"] == "tok-x" and seen["auth"] == "query_token"


def test_s50_resolve_bot_username_gitee_from_card(gitee_card):
    """resolve_bot_username：config-only 平台取卡 bot_identity（B2 语义）。"""
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    assert resolve_bot_username("gitee") == "g_grape_bot"
    # gitcode 无卡场景下走全局 meta 链（存量行为，回归）
    assert isinstance(resolve_bot_username("gitcode"), str)
