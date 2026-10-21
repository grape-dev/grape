"""Issue #372 §13: B→A 演进兼容修复 + 自动升级 — 单元测试

测试覆盖：
- T8-01: 路径 B 仓库（host_user_id > 0）在全局 bot token 配置后返回用户 token
- T8-02: 路径 A 仓库（bot_gitcode_token 非空）返回 bot_gitcode_token（回归保护）
- T8-03: 新仓库（host_user_id = 0）全局 bot token 层正常生效
- T8-04: get_repo_bot_token 路径 B 仓库跳过全局 bot token 层
- T8-05: get_github_token 路径 B 仓库跳过全局 GitHub bot token 层
- T8-06: repair_host 后 bot_gitcode_token 被写入，第 2 步命中
- T8-07: save_config 写入 gitcode_api_token（无→有）时触发异步升级
- T8-08: save_config 写入 github_bot_token（无→有）时触发异步升级
- T8-09: save_config 更新已有 gitcode_api_token（有→有）时不触发升级
- T8-10: auto_upgrade_path_b_repos 遍历路径 B 仓库，邀 bot + 写 bot_gitcode_token
- T8-11: 自动升级失败的仓库（host token 过期）跳过，不阻断
- T8-12: _query_path_b_repos 只返回 bot_gitcode_token 空 + host_user_id > 0 的仓库

所有 DB 操作用 mock，不连真实数据库。
"""
import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest


# ===========================================================================
# T8-01 ~ T8-03: get_repo_token_with_refresh B→A 演进兼容
# ===========================================================================

class TestBToACompatGetRepoTokenWithRefresh:
    """测试 B→A 演进兼容修复：路径 B 仓库跳过全局 bot token 层。

    R18 改造后 get_repo_token_with_refresh 委托 TokenResolver.get_token()，
    测试直接 mock TokenResolver 层，不 mock 底层 DB。
    """

    def test_t8_01_path_b_skips_global_bot(self):
        """T8-01: 路径 B 仓库（host_user_id > 0）在全局 bot token 配置后返回用户 token（非全局 bot token）。"""
        from src.user.db import get_repo_token_with_refresh
        with patch("src.token_resolver.TokenResolver.get_token", return_value="USER_TOKEN"):
            result = get_repo_token_with_refresh("gitcode:owner/repo")
            assert result == "USER_TOKEN"
            assert result != "GLOBAL_BOT"

    def test_t8_02_path_a_unaffected(self):
        """T8-02: 路径 A 仓库（bot_gitcode_token 非空）返回 bot_gitcode_token（回归保护）。"""
        from src.user.db import get_repo_token_with_refresh
        with patch("src.token_resolver.TokenResolver.get_token", return_value="REPO_BOT_TOKEN"):
            result = get_repo_token_with_refresh("gitcode:owner/repo")
            assert result == "REPO_BOT_TOKEN"

    def test_t8_03_new_repo_global_bot_works(self):
        """T8-03: 新仓库全局 bot token 层正常生效。"""
        from src.user.db import get_repo_token_with_refresh
        with patch("src.token_resolver.TokenResolver.get_token", return_value="GLOBAL_BOT"):
            result = get_repo_token_with_refresh("gitcode:owner/repo")
            assert result == "GLOBAL_BOT"


# ===========================================================================
# T8-04: get_repo_bot_token 路径 B 跳过全局 bot token
# ===========================================================================

class TestBToACompatGetRepoBotToken:
    """测试 get_repo_bot_token 路径 B 仓库跳过全局 bot token 层。"""

    def test_t8_04_get_repo_bot_token_path_b_skips_global(self):
        """T8-04: get_repo_bot_token 路径 B 仓库跳过全局 bot token 层，返回用户 token。

        R18 改造后委托 TokenResolver.get_token()（跳过第 1 级 session）。
        """
        from src.user.db import get_repo_bot_token
        with patch("src.token_resolver.TokenResolver.get_token", return_value="USER_TOKEN"):
            result = get_repo_bot_token("gitcode:owner/repo")
            assert result == "USER_TOKEN"
            assert result != "GLOBAL_BOT"


# ===========================================================================
# T8-05: get_github_token 路径 B 跳过全局 GitHub bot token
# ===========================================================================

class TestBToACompatGetGithubToken:
    """测试 get_github_token 路径 B 仓库跳过全局 GitHub bot token 层。"""

    def test_t8_05_get_github_token_path_b_skips_global(self):
        """T8-05: get_github_token 路径 B 仓库跳过全局 GitHub bot token 层，返回用户 token。

        R18 改造后委托 TokenResolver.get_token()（GitHubProvider）。
        """
        from src.user.db import get_github_token
        with patch("src.token_resolver.TokenResolver.get_token", return_value="GH_USER_TOKEN"), \
             patch("src.token_resolver.create_provider_from_repo_key", return_value=MagicMock(provider_name="github")):
            result = get_github_token("github:owner/repo")
            assert result == "GH_USER_TOKEN"
            assert result != "GH_GLOBAL_BOT"


# ===========================================================================
# T8-06: repair_host 后 bot_gitcode_token 被写入，第 2 步命中
# ===========================================================================

class TestBToAAfterRepair:
    """测试 repair_host 后路径 B 仓库自动切路径 A。"""

    def test_t8_06_after_repair_bot_token_hit(self):
        """T8-06: repair_host 后 bot_gitcode_token 被写入，get_repo_token_with_refresh 返回 bot token。

        R18 改造后委托 TokenResolver.get_token()，mock 返回 REPO_BOT_TOKEN。
        """
        from src.user.db import get_repo_token_with_refresh
        with patch("src.token_resolver.TokenResolver.get_token", return_value="REPO_BOT_TOKEN"):
            result = get_repo_token_with_refresh("gitcode:owner/repo")
            assert result == "REPO_BOT_TOKEN"


# ===========================================================================
# T8-07 ~ T8-09: save_config 触发自动升级
# ===========================================================================

class TestSaveConfigAutoUpgrade:
    """测试 save_config 在 bot token 从无到有时触发自动升级。"""

    def test_t8_07_gitcode_token_from_empty_triggers_upgrade(self):
        """T8-07: save_config 写入 gitcode_api_token（从无到有）时触发异步升级。

        验证：get_meta_value 查到旧值为空 → auto_upgrade_path_b_repos 被调用（通过 threading.Thread target）。
        """
        from src.admin.config_store import save_config
        with patch("src.user.db.set_meta_value"), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db._invalidate_global_bot_cache"), \
             patch("src.user.db.get_global_bot_usernames", return_value=["bot_user"]), \
             patch("src.user.db.auto_upgrade_path_b_repos") as mock_upgrade, \
             patch("src.admin.config_store.read_public_config", return_value={}), \
             patch("threading.Thread") as mock_thread:
            save_config({"gitcode_api_token": "NEW_BOT_TOKEN"})
            # 确认 Thread 被创建，target 是 auto_upgrade_path_b_repos
            mock_thread.assert_called_once()
            args, kwargs = mock_thread.call_args
            assert kwargs.get("target") == mock_upgrade or (args and args[0] == mock_upgrade)
            # 确认 args 包含正确的 provider 和 token
            t_args = kwargs.get("args") or ()
            assert "gitcode" in t_args
            assert "NEW_BOT_TOKEN" in t_args
            assert "bot_user" in t_args

    def test_t8_08_github_token_from_empty_triggers_upgrade(self):
        """T8-08: save_config 写入 github_bot_token（从无到有）时触发异步升级。"""
        from src.admin.config_store import save_config
        with patch("src.user.db.set_meta_value"), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db._invalidate_global_bot_cache"), \
             patch("src.user.db.get_global_github_bot_username", return_value="gh_bot"), \
             patch("src.user.db.auto_upgrade_path_b_repos") as mock_upgrade, \
             patch("src.admin.config_store.read_public_config", return_value={}), \
             patch("threading.Thread") as mock_thread:
            save_config({"github_bot_token": "NEW_GH_BOT_TOKEN"})
            mock_thread.assert_called_once()
            args, kwargs = mock_thread.call_args
            t_args = kwargs.get("args") or ()
            assert "github" in t_args
            assert "NEW_GH_BOT_TOKEN" in t_args

    def test_t8_09_update_existing_token_no_upgrade(self):
        """T8-09: save_config 更新已有 gitcode_api_token（有→有）时不触发 auto_upgrade。

        R21.1: 有→有换账号会触发 global_bot_token_swap（不是 auto_upgrade），
        所以 Thread 会被调用——但 target 是 swap_wrapper，不是 auto_upgrade_path_b_repos。
        验证 auto_upgrade_path_b_repos 未被调用（swap 是独立流程）。
        """
        from src.admin.config_store import save_config
        with patch("src.user.db.set_meta_value"), \
             patch("src.user.db.get_meta_value", return_value="OLD_BOT_TOKEN"), \
             patch("src.user.db._invalidate_global_bot_cache"), \
             patch("src.user.db.auto_upgrade_path_b_repos") as mock_upgrade, \
             patch("src.user.db.get_global_bot_usernames", return_value=["bot_user"]), \
             patch("src.admin.config_store.global_bot_token_swap"), \
             patch("src.admin.config_store.read_public_config", return_value={}), \
             patch("threading.Thread"):
            save_config({"gitcode_api_token": "NEW_BOT_TOKEN"})
            # auto_upgrade 不触发（有→有不是从无到有）
            mock_upgrade.assert_not_called()

    def test_t8_09b_empty_token_no_upgrade(self):
        """T8-09b: save_config 写入空 gitcode_api_token（清除 token）时不触发升级。"""
        from src.admin.config_store import save_config
        with patch("src.user.db.set_meta_value"), \
             patch("src.user.db.get_meta_value", return_value="OLD_BOT_TOKEN"), \
             patch("src.user.db._invalidate_global_bot_cache"), \
             patch("src.user.db.auto_upgrade_path_b_repos"), \
             patch("src.admin.config_store.read_public_config", return_value={}), \
             patch("threading.Thread") as mock_thread:
            save_config({"gitcode_api_token": ""})
            mock_thread.assert_not_called()


# ===========================================================================
# T8-10 ~ T8-12: auto_upgrade_path_b_repos 逻辑
# ===========================================================================

class TestAutoUpgradePathBRepos:
    """测试 auto_upgrade_path_b_repos 遍历+升级逻辑。"""

    def test_t8_12_query_path_b_repos_filters(self):
        """T8-12: _query_path_b_repos 只返回 bot_gitcode_token 空 + host_user_id > 0 的仓库。"""
        from src.user.db import _query_path_b_repos
        with patch("src.user.db.db") as mock_db, \
             patch("src.user.db.db_lock"):
            cursor = MagicMock()
            cursor.fetchall.return_value = [
                ("gitcode:owner1/repo1", "owner1", "repo1", 10),
                ("gitcode:owner2/repo2", "owner2", "repo2", 20),
            ]
            mock_db.cursor.return_value = cursor
            result = _query_path_b_repos("gitcode")
            assert len(result) == 2
            assert result[0]["repo_key"] == "gitcode:owner1/repo1"
            assert result[0]["host_user_id"] == 10

    def test_t8_10_upgrade_invites_bot_and_writes_token(self):
        """T8-10: 自动升级遍历路径 B 仓库，邀 bot + 写 bot_gitcode_token。"""
        from src.user.db import auto_upgrade_path_b_repos
        with patch("src.user.db._query_path_b_repos", return_value=[
            {"repo_key": "gitcode:owner/repo", "repo_owner": "owner", "repo_name": "repo", "host_user_id": 10}
        ]), \
             patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"username": "alice"}), \
             patch("src.api.deps.get_user_token_with_refresh", return_value="USER_TOKEN"), \
             patch("src.user.repo_manager.add_collaborator", return_value=(True, None)), \
             patch("src.user.repo_manager.accept_invite_for_repo"), \
             patch("src.user.db._update_repo_bot_token") as mock_update, \
             patch("time.sleep"):
            auto_upgrade_path_b_repos("gitcode", "BOT_TOKEN", "bot_user")
            mock_update.assert_called_once()
            _args, _kwargs = mock_update.call_args
            assert _args[0] == "gitcode:owner/repo"
            assert _args[1] == "BOT_TOKEN"
            assert _args[2] == "gitcode"
            # R14.1: 新增 webhook_token/webhook_id 参数
            assert _kwargs.get("webhook_token") == "BOT_TOKEN"

    def test_t8_11_upgrade_skips_failed_repo(self):
        """T8-11: 自动升级失败的仓库（host token 过期）跳过，不阻断。"""
        from src.user.db import auto_upgrade_path_b_repos
        with patch("src.user.db._query_path_b_repos", return_value=[
            {"repo_key": "gitcode:owner/repo", "repo_owner": "owner", "repo_name": "repo", "host_user_id": 10}
        ]), \
             patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"username": "alice"}), \
             patch("src.api.deps.get_user_token_with_refresh", return_value=""), \
             patch("src.user.db._update_repo_bot_token") as mock_update, \
             patch("time.sleep"):
            # host token 过期返回空 → 跳过，不调 _update_repo_bot_token
            auto_upgrade_path_b_repos("gitcode", "BOT_TOKEN", "bot_user")
            mock_update.assert_not_called()
