"""探活 404 误判修复测试（GitHub hooks-404 权限问题）。

m2 环境事故：GitHub 对「token 有效但无 admin 权限」查 /hooks 回 404，
探活第二步把 404 误判为仓库/webhook 失效，级联清空了 6 个 GitHub 托管仓。
GitCode 对无权限回 403（旧逻辑已正确跳过），只有 GitHub 分支受影响。

🔴 红阶段：修复前 FAIL——
- GitHub 仓库本体 200 + hooks 404（非 admin）被误判 stale 并级联清理
🟢 绿阶段：修复后 PASS。

敏感信息：无真实仓库名/IP/凭据（使用测试数据与 mock）。
"""
import os
import sys

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

import urllib.error
import urllib.request
from unittest.mock import patch

from src.api.routes.repos import _probe_stale_repos

GH_USER_TOKEN = "gh-user-token"
BOT_TOKEN = "bot-token"


class _FakeResp:
    def __init__(self, body=b"[]"):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_factory(user_routes, bot_routes=None):
    """按 (URL, token) 返回预设响应。

    user_routes: {url: status 或 (status, body)}——用户 token 请求的响应。
    bot_routes: 同结构——bot token 请求的响应（FR-1.2 二次验证），缺省复用 user_routes。
    bot token 通过 Authorization header 中的 BOT_TOKEN 识别。
    """
    bot_routes = bot_routes if bot_routes is not None else user_routes

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        auth = req.headers.get("Authorization", "")
        routes = bot_routes if BOT_TOKEN in auth else user_routes
        if url not in routes:
            raise AssertionError(f"unexpected url: {url}")
        v = routes[url]
        if isinstance(v, tuple):
            code, body = v
        else:
            code, body = v, b"[]"
        if code != 200:
            raise urllib.error.HTTPError(url, code, "err", {}, None)
        return _FakeResp(body)

    return _fake_urlopen


def _run_probe(user_id, repo_key, user_routes, bot_routes=None):
    """跑一轮探活，mock 掉 DB / 用户 GitHub token / 网络层，返回 user_db mock。"""
    owner, name = repo_key.split(":")[1].split("/")
    with patch("src.api.routes.repos.user_db") as udb, \
         patch("src.api.deps.get_user_github_token_with_refresh", return_value=GH_USER_TOKEN), \
         patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"username": "t"}), \
         patch("urllib.request.urlopen", side_effect=_urlopen_factory(user_routes, bot_routes)):
        udb.get_managed_repos.return_value = [{
            "repo_key": repo_key, "repo_owner": owner, "repo_name": name,
            "webhook_id": 123}]
        udb.set_host_status.return_value = None
        udb.remove_managed_repo.return_value = None
        user_routes.setdefault("https://gitcode.com/api/v5/user", 200)
        user_routes.setdefault("https://api.github.com/user", 200)
        _probe_stale_repos("gc-token", user_id)
    return udb


GH_REPO = "https://api.github.com/repos/testorg/testrepo"
GH_HOOKS = GH_REPO + "/hooks"
GC_REPO = "https://gitcode.com/api/v5/repos/testorg/testrepo"
GC_HOOKS = GC_REPO + "/hooks"


class TestGithubHooks404Permission:

    def test_github_repo_exists_hooks_404_not_cleaned(self):
        """GitHub 仓库本体 200 + hooks 404（非 admin 用户）→ 不级联清理（本次修复核心）。"""
        with patch("src.repo_cleanup.cascade_cleanup_repo_data") as cc:
            udb = _run_probe(1, "github:testorg/testrepo",
                             {GH_REPO: 200, GH_HOOKS: 404})
            udb.remove_managed_repo.assert_not_called()
            cc.assert_not_called()

    def test_github_hooks_404_not_marked_webhook_missing_either(self):
        """GitHub hooks 404（权限问题）连待修复状态都不该置——hook 大概率原样还在。"""
        udb = _run_probe(1, "github:testorg/testrepo",
                         {GH_REPO: 200, GH_HOOKS: 404})
        udb.set_host_status.assert_not_called()

    def test_github_repo_404_on_step1_still_cleaned_after_bot_confirm(self):
        """GitHub 仓库本体 404（真删除）+ bot token 也 404 → 双信号确认后仍清理。"""
        from src.token_resolver import TokenResolver
        with patch.object(TokenResolver, "read_stored_token", return_value=BOT_TOKEN), \
             patch("src.repo_cleanup.cascade_cleanup_repo_data") as cc:
            udb = _run_probe(1, "github:testorg/testrepo",
                             {GH_REPO: 404}, {GH_REPO: 404})
            cc.assert_called_once()
            udb.remove_managed_repo.assert_called_once_with("github:testorg/testrepo")

    def test_github_repo_404_no_bot_token_downgrades_to_pending_repair(self):
        """GitHub 仓库 404 但无 bot token 二次验证 → 降级 webhook_missing，不清理。"""
        from src.token_resolver import TokenResolver
        with patch.object(TokenResolver, "read_stored_token", return_value=""), \
             patch("src.repo_cleanup.cascade_cleanup_repo_data") as cc:
            udb = _run_probe(1, "github:testorg/testrepo",
                             {GH_REPO: 404}, {})
            cc.assert_not_called()
            udb.remove_managed_repo.assert_not_called()
            udb.set_host_status.assert_called_once_with("github:testorg/testrepo", "webhook_missing")


class TestGitcodeHooks404Unchanged:

    def test_gitcode_repo_exists_hooks_404_still_stale(self):
        """GitCode 仓库本体 200 + hooks 404 → 保持旧行为（bot 二次确认 404 后清理）。"""
        from src.token_resolver import TokenResolver
        with patch.object(TokenResolver, "read_stored_token", return_value=BOT_TOKEN), \
             patch("src.repo_cleanup.cascade_cleanup_repo_data") as cc:
            udb = _run_probe(1, "gitcode:testorg/testrepo",
                             {GC_REPO: 200, GC_HOOKS: 404}, {GC_REPO: 404})
            cc.assert_called_once()
            udb.remove_managed_repo.assert_called_once_with("gitcode:testorg/testrepo")

    def test_gitcode_hooks_403_skipped(self):
        """GitCode hooks 403（无权限）→ 跳过不判（回归保护，旧行为）。"""
        udb = _run_probe(1, "gitcode:testorg/testrepo",
                         {GC_REPO: 200, GC_HOOKS: 403})
        udb.remove_managed_repo.assert_not_called()
        udb.set_host_status.assert_not_called()
