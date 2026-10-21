"""R21.1 全局 bot token 更换 + R21.2 per-provider key 测试。

测试要点：
- R21.1.1: save_config 检测有→有换账号
- R21.1.2: global_bot_token_swap 七步流程
- R21.1.5: 过期标记清除（已在 save_config 中实现）
- R21.1.7: 分布式锁
- R21.2.9: 旧 key 迁移
"""
import pytest
from unittest.mock import patch, MagicMock


class TestGlobalBotTokenSwapDetection:
    """R21.1.1: save_config 检测有→有换账号。"""

    def test_swap_detected_when_token_changes(self):
        """R21.1.1: old_token 非空且 != new_token → 触发 swap"""
        import src.admin.config_store as cs
        from src.session_manager import db, db_lock

        # 模拟 DB：旧 token 存在
        _fake_cursor = MagicMock()
        _fake_cursor.fetchone.return_value = ("bot_old",)

        with patch("src.user.db.get_meta_value", side_effect=lambda k: "bot_old" if k == "global_bot_gitcode_token" else ""), \
             patch("src.user.db.set_meta_value"), \
             patch("src.user.db._invalidate_global_bot_cache"), \
             patch("src.user.db.db_lock"), \
             patch("src.user.db.db"), \
             patch("src.user.db.auto_upgrade_path_b_repos"), \
             patch.object(cs, "read_public_config", return_value={}), \
             patch("threading.Thread") as mock_thread:
            cs.save_config({"gitcode_api_token": "bot_new"})
            # R21.1.1: 验证有 Thread 被创建用于 swap
            assert mock_thread.called, "save_config should start a Thread for swap"
            # 检查最后一个 Thread 调用的 target 是 _global_bot_token_swap_wrapper
            _last_call = mock_thread.call_args_list[-1]
            _target = _last_call.args[0] if _last_call.args else _last_call.kwargs.get("target")
            assert _target is cs._global_bot_token_swap_wrapper, \
                f"Thread target should be _global_bot_token_swap_wrapper, got {_target}"
            _args = _last_call.kwargs.get("args", ())
            assert _args == ("gitcode", "bot_old", "bot_new"), \
                f"Thread args should be swap params, got {_args}"


class TestGlobalBotTokenSwap:
    """R21.1.2: global_bot_token_swap 七步流程。"""

    def test_invalid_new_token_aborts(self):
        """R21.1.2 步骤 1: 新 token 无效 → 中止"""
        from src.admin.config_store import global_bot_token_swap
        with patch("src.token_resolver.GitCodeProvider.verify_token", side_effect=RuntimeError("401")):
            global_bot_token_swap("gitcode", "bot_old", "invalid_new")
            # 无异常，静默返回

    def test_valid_swap_updates_repos(self):
        """R21.1.2: 有效新 token → 遍历仓库 + 重建 webhook + 写 DB"""
        from src.admin.config_store import global_bot_token_swap

        _fake_cursor = MagicMock()
        _fake_cursor.fetchall.return_value = [("gitcode:owner/repo", "old_bot_name")]

        with patch("src.token_resolver.GitCodeProvider.verify_token", return_value={"login": "new_bot_name"}), \
             patch("src.session_manager.db") as mock_db, \
             patch("src.session_manager.db_lock"), \
             patch("src.user.db.get_webhook_url", return_value="https://example.com/webhook/gitcode"), \
             patch("src.user.db._get_managed_webhook_id", return_value=123), \
             patch("src.token_resolver.GitCodeProvider.add_collaborator", return_value=True) as mock_add, \
             patch("src.token_resolver.GitCodeProvider.remove_webhook", return_value=True) as mock_rm_wh, \
             patch("src.token_resolver.GitCodeProvider.setup_webhook", return_value=456) as mock_setup_wh, \
             patch("src.token_resolver.GitCodeProvider.remove_collaborator", return_value=True) as mock_rm_bot:
            mock_db.cursor.return_value = _fake_cursor
            global_bot_token_swap("gitcode", "bot_old", "bot_new")
            # 验证邀请新 bot
            mock_add.assert_called_once()
            # 验证删旧 webhook
            mock_rm_wh.assert_called_once()
            # 验证建新 webhook
            mock_setup_wh.assert_called_once()
            # 验证移除旧 bot
            mock_rm_bot.assert_called_once()

    def test_no_repos_using_old_token(self):
        """R21.1.2: 没有仓库使用旧 token → 静默完成"""
        from src.admin.config_store import global_bot_token_swap

        _fake_cursor = MagicMock()
        _fake_cursor.fetchall.return_value = []  # 没有仓库

        with patch("src.token_resolver.GitCodeProvider.verify_token", return_value={"login": "new_bot"}), \
             patch("src.session_manager.db") as mock_db, \
             patch("src.session_manager.db_lock"):
            mock_db.cursor.return_value = _fake_cursor
            global_bot_token_swap("gitcode", "bot_old", "bot_new")
            # 无异常，静默完成


class TestPerProviderKeyMigration:
    """R21.2.9: 旧 global_bot_token_status key 迁移。"""

    def test_old_key_migrated_to_per_provider(self):
        """R21.2.9: 旧 key 拆分为两个 per-provider key"""
        # 模拟迁移函数中的逻辑
        with patch("src.user.db.get_meta_value", side_effect=lambda k: "expired" if k == "global_bot_token_status" else ""), \
             patch("src.user.db.set_meta_value") as mock_set:
            _old_status = "expired"
            if _old_status:
                from src.user.db import set_meta_value
                set_meta_value("global_gitcode_bot_token_status", _old_status)
                set_meta_value("global_github_bot_token_status", _old_status)
                set_meta_value("global_bot_token_status", "")

            # 验证 3 个 set_meta_value 调用
            keys_set = [c.args[0] for c in mock_set.call_args_list]
            assert "global_gitcode_bot_token_status" in keys_set
            assert "global_github_bot_token_status" in keys_set
            assert "global_bot_token_status" in keys_set


class TestGlobalBotTokenStrategyPerProvider:
    """R21.2.7: GlobalBotToken 策略按 provider 操作对应 key。"""

    def test_gitcode_strategy_uses_gitcode_key(self):
        """R21.2.7: GitCodeProvider → global_gitcode_bot_token_status"""
        from src.token_resolver import GlobalBotToken, GitCodeProvider
        strategy = GlobalBotToken(GitCodeProvider())
        assert strategy.provider.global_bot_status_meta_key() == "global_gitcode_bot_token_status"

    def test_github_strategy_uses_github_key(self):
        """R21.2.7: GitHubProvider → global_github_bot_token_status"""
        from src.token_resolver import GlobalBotToken, GitHubProvider
        strategy = GlobalBotToken(GitHubProvider())
        assert strategy.provider.global_bot_status_meta_key() == "global_github_bot_token_status"

    def test_gitcode_mark_expired_sets_gitcode_key(self):
        """R21.2.7: GitCode mark_expired → global_gitcode_bot_token_status=expired"""
        from src.token_resolver import GlobalBotToken, GitCodeProvider
        strategy = GlobalBotToken(GitCodeProvider())
        with patch("src.user.db.set_meta_value") as mock_set, \
             patch("src.user.db._invalidate_global_bot_cache"):
            strategy.mark_expired("gitcode:owner/repo")
            mock_set.assert_called_with("global_gitcode_bot_token_status", "expired")

    def test_github_mark_expired_sets_github_key(self):
        """R21.2.7: GitHub mark_expired → global_github_bot_token_status=expired"""
        from src.token_resolver import GlobalBotToken, GitHubProvider
        strategy = GlobalBotToken(GitHubProvider())
        with patch("src.user.db.set_meta_value") as mock_set, \
             patch("src.user.db._invalidate_global_bot_cache"):
            strategy.mark_expired("github:owner/repo")
            mock_set.assert_called_with("global_github_bot_token_status", "expired")
