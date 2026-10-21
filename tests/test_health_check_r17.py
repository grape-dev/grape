"""R17 健康检查测试。

测试要点：
- R17.2.1: CHECK_INTERVAL=3600, FAILURE_THRESHOLD=1
- R17.2.2: 401 → 标记 per-provider 过期 key
- R17.2.3: 200 → 清除过期标记
- R17.2.5: 仓库级 401 → 标记 bot_token_status
- R17.2.6: 仓库级 200 → 清除标记
- R21.3.4: 超时/5xx 不计入 FAILURE_THRESHOLD
- R21.3.5: detect_and_mark_expired 区分 401 和超时
"""
import pytest
from unittest.mock import patch, MagicMock


class TestHealthCheckConfig:
    """R17.2.1: 健康检查配置。"""

    def test_check_interval_is_3600(self):
        from src.runtime.bot_token_health import CHECK_INTERVAL
        assert CHECK_INTERVAL == 3600, "R17.2.1: CHECK_INTERVAL should be 3600 (1 hour)"

    def test_failure_threshold_is_1(self):
        from src.runtime.bot_token_health import FAILURE_THRESHOLD
        assert FAILURE_THRESHOLD == 1, "R21.3.4: FAILURE_THRESHOLD should be 1 (1x 401 marks expired)"


class TestGlobalTokenHealthCheck:
    """R17.2.2/R17.2.3: 全局 bot token 健康检查标记/清除。"""

    def test_401_marks_expired(self):
        """R17.2.2: 401 → 标记 per-provider 过期 key。"""
        from src.runtime.bot_token_health import _update_global_token_status
        with patch("src.user.db.set_meta_value") as mock_set, \
             patch("src.user.db._invalidate_global_bot_cache"):
            _update_global_token_status("gitcode", {"healthy": False, "error_type": "auth", "error": "401"})
            mock_set.assert_called_with("global_gitcode_bot_token_status", "expired")

    def test_200_clears_expired(self):
        """R17.2.3: 200 → 清除过期标记。"""
        from src.runtime.bot_token_health import _update_global_token_status
        with patch("src.user.db.set_meta_value") as mock_set, \
             patch("src.user.db._invalidate_global_bot_cache"):
            _update_global_token_status("github", {"healthy": True, "error_type": "", "error": ""})
            mock_set.assert_called_with("global_github_bot_token_status", "")

    def test_network_error_does_not_mark(self):
        """R21.3.7: 超时/5xx → 不改变标记状态。"""
        from src.runtime.bot_token_health import _update_global_token_status
        with patch("src.user.db.set_meta_value") as mock_set, \
             patch("src.user.db._invalidate_global_bot_cache"):
            _update_global_token_status("gitcode", {"healthy": False, "error_type": "network", "error": "timeout"})
            mock_set.assert_not_called()

    def test_per_provider_keys(self):
        """R21.2: GitCode 和 GitHub 使用不同的 per-provider key。"""
        from src.runtime.bot_token_health import _update_global_token_status
        with patch("src.user.db.set_meta_value") as mock_set, \
             patch("src.user.db._invalidate_global_bot_cache"):
            _update_global_token_status("gitcode", {"healthy": False, "error_type": "auth", "error": "401"})
            _update_global_token_status("github", {"healthy": False, "error_type": "auth", "error": "401"})
            keys_called = [c.args[0] for c in mock_set.call_args_list]
            assert "global_gitcode_bot_token_status" in keys_called
            assert "global_github_bot_token_status" in keys_called


class TestRepoTokenHealthCheck:
    """R17.2.5/R17.2.6: 仓库级 bot token 健康检查。"""

    def test_repo_401_marks_expired(self):
        """R17.2.5: 仓库级 401 → 标记 bot_token_status=expired。"""
        from src.runtime.bot_token_health import _verify_single_repo_token
        with patch("src.session_manager.db") as mock_db, \
             patch("src.session_manager.db_lock"), \
             patch("src.token_resolver.GitCodeProvider.verify_token", side_effect=RuntimeError("failed (401)")), \
             patch("src.runtime.bot_token_health._set_repo_bot_token_status") as mock_set_status:
            _cursor = MagicMock()
            _cursor.fetchone.return_value = ("bot_token_value",)
            mock_db.cursor.return_value = _cursor
            _verify_single_repo_token("gitcode:owner/repo", "gitcode")
            mock_set_status.assert_called_with("gitcode:owner/repo", "expired")

    def test_repo_200_clears_expired(self):
        """R17.2.6: 仓库级 200 → 清除过期标记。"""
        from src.runtime.bot_token_health import _verify_single_repo_token
        with patch("src.session_manager.db") as mock_db, \
             patch("src.session_manager.db_lock"), \
             patch("src.token_resolver.GitCodeProvider.verify_token", return_value={"login": "bot"}), \
             patch("src.runtime.bot_token_health._set_repo_bot_token_status") as mock_set_status:
            _cursor = MagicMock()
            _cursor.fetchone.return_value = ("bot_token_value",)
            mock_db.cursor.return_value = _cursor
            _verify_single_repo_token("gitcode:owner/repo", "gitcode")
            mock_set_status.assert_called_with("gitcode:owner/repo", "")

    def test_repo_network_error_does_not_mark(self):
        """R21.3.7: 仓库级超时/5xx → 不标记。"""
        from src.runtime.bot_token_health import _verify_single_repo_token
        with patch("src.session_manager.db") as mock_db, \
             patch("src.session_manager.db_lock"), \
             patch("src.token_resolver.GitCodeProvider.verify_token", side_effect=RuntimeError("timeout")), \
             patch("src.runtime.bot_token_health._set_repo_bot_token_status") as mock_set_status:
            _cursor = MagicMock()
            _cursor.fetchone.return_value = ("bot_token_value",)
            mock_db.cursor.return_value = _cursor
            _verify_single_repo_token("gitcode:owner/repo", "gitcode")
            mock_set_status.assert_not_called()


class TestDetectAndMarkExpiredR21:
    """R21.3.5: detect_and_mark_expired 区分 401 和超时。"""

    def test_401_marks_expired(self):
        """R21.3.5: verify_token 401 → 标记过期，返回 error_code。"""
        from src.token_resolver import TokenResolver, create_provider
        _resolver = TokenResolver(create_provider("gitcode"))
        with patch("src.token_resolver.DevopsAgentSessionToken._fetch", return_value="some_token"), \
             patch("src.token_resolver.GitCodeProvider.verify_token", side_effect=RuntimeError("failed (401)")), \
             patch("src.token_resolver.DevopsAgentSessionToken._is_expired", return_value=False), \
             patch("src.token_resolver.DevopsAgentSessionToken.mark_expired") as mock_mark:
            result = _resolver.detect_and_mark_expired("gitcode:owner/repo")
            assert result == "SESSION_TOKEN_EXPIRED"
            mock_mark.assert_called_once()

    def test_timeout_does_not_mark(self):
        """R21.3.5: verify_token 超时 → 不标记，返回空。"""
        from src.token_resolver import TokenResolver, create_provider
        _resolver = TokenResolver(create_provider("gitcode"))
        with patch("src.token_resolver.DevopsAgentSessionToken._fetch", return_value="some_token"), \
             patch("src.token_resolver.GitCodeProvider.verify_token", side_effect=RuntimeError("connection timeout")), \
             patch("src.token_resolver.DevopsAgentSessionToken._is_expired", return_value=False), \
             patch("src.token_resolver.DevopsAgentSessionToken.mark_expired") as mock_mark:
            result = _resolver.detect_and_mark_expired("gitcode:owner/repo")
            assert result == ""
            mock_mark.assert_not_called()

    def test_5xx_does_not_mark(self):
        """R21.3.5: verify_token 5xx → 不标记，返回空。"""
        from src.token_resolver import TokenResolver, create_provider
        _resolver = TokenResolver(create_provider("gitcode"))
        with patch("src.token_resolver.DevopsAgentSessionToken._fetch", return_value="some_token"), \
             patch("src.token_resolver.GitCodeProvider.verify_token", side_effect=RuntimeError("failed (503)")), \
             patch("src.token_resolver.DevopsAgentSessionToken._is_expired", return_value=False), \
             patch("src.token_resolver.DevopsAgentSessionToken.mark_expired") as mock_mark:
            result = _resolver.detect_and_mark_expired("gitcode:owner/repo")
            assert result == ""
            mock_mark.assert_not_called()
