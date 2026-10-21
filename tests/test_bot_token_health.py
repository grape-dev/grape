"""Issue #345 方向1：bot token 健康探活测试。

Tests cover:
- _verify_bot_token: per-platform healthy/auth/network/config
- _verify_global_bot_token: returns per-platform results
- _update_health_status: per-platform failure counting, config skip,
  WorstOf aggregation (healthy/degraded/unhealthy)
- bot_token_health_loop: lock acquire/release, exception handling
- start_bot_token_health: idempotent startup
"""
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio


class TestVerifyBotToken:
    """Per-platform token verification tests."""

    def test_gitcode_healthy(self):
        with patch("src.user.db.get_global_gitcode_bot_token", return_value="valid_gc_token"), \
             patch("src.user.repo_manager._req", return_value={"login": "bot"}):
            from src.runtime.bot_token_health import _verify_bot_token
            result = _verify_bot_token("gitcode")
        assert result["healthy"] is True
        assert result["error_type"] == ""

    def test_github_healthy(self):
        with patch("src.user.db.get_global_github_bot_token", return_value="valid_gh_token"), \
             patch("src.user.repo_manager._github_req", return_value={"login": "bot"}):
            from src.runtime.bot_token_health import _verify_bot_token
            result = _verify_bot_token("github")
        assert result["healthy"] is True
        assert result["error_type"] == ""

    def test_gitcode_401(self):
        with patch("src.user.db.get_global_gitcode_bot_token", return_value="bad_token"), \
             patch("src.user.repo_manager._req", side_effect=RuntimeError("failed (401): Unauthorized")):
            from src.runtime.bot_token_health import _verify_bot_token
            result = _verify_bot_token("gitcode")
        assert result["healthy"] is False
        assert result["error_type"] == "auth"

    def test_github_401(self):
        with patch("src.user.db.get_global_github_bot_token", return_value="bad_gh_token"), \
             patch("src.user.repo_manager._github_req", side_effect=RuntimeError("failed (401): Bad credentials")):
            from src.runtime.bot_token_health import _verify_bot_token
            result = _verify_bot_token("github")
        assert result["healthy"] is False
        assert result["error_type"] == "auth"

    def test_gitcode_network_error(self):
        with patch("src.user.db.get_global_gitcode_bot_token", return_value="valid_token"), \
             patch("src.user.repo_manager._req", side_effect=RuntimeError("Cannot reach GitCode API: timeout")):
            from src.runtime.bot_token_health import _verify_bot_token
            result = _verify_bot_token("gitcode")
        assert result["healthy"] is False
        assert result["error_type"] == "network"

    def test_github_network_error(self):
        with patch("src.user.db.get_global_github_bot_token", return_value="valid_gh_token"), \
             patch("src.user.repo_manager._github_req", side_effect=RuntimeError("Cannot reach GitHub API: timeout")):
            from src.runtime.bot_token_health import _verify_bot_token
            result = _verify_bot_token("github")
        assert result["healthy"] is False
        assert result["error_type"] == "network"

    def test_gitcode_not_configured(self):
        with patch("src.user.db.get_global_gitcode_bot_token", return_value=""):
            from src.runtime.bot_token_health import _verify_bot_token
            result = _verify_bot_token("gitcode")
        assert result["healthy"] is False
        assert result["error_type"] == "config"
        assert "GitCode" in result["error"]

    def test_github_not_configured(self):
        with patch("src.user.db.get_global_github_bot_token", return_value=""):
            from src.runtime.bot_token_health import _verify_bot_token
            result = _verify_bot_token("github")
        assert result["healthy"] is False
        assert result["error_type"] == "config"
        assert "GitHub" in result["error"]


class TestVerifyGlobalBotToken:
    """Aggregate verification — returns per-platform results."""

    def test_both_healthy(self):
        """Both platforms healthy → platforms dict has both with healthy=True."""
        with patch("src.runtime.bot_token_health._verify_bot_token") as mock_verify:
            mock_verify.side_effect = lambda p: {"healthy": True, "error": "", "error_type": ""}
            from src.runtime.bot_token_health import _verify_global_bot_token
            result = _verify_global_bot_token()
        assert "platforms" in result
        assert result["platforms"]["gitcode"]["healthy"] is True
        assert result["platforms"]["github"]["healthy"] is True

    def test_one_fails(self):
        """GitCode healthy, GitHub fails → both in result, each independent."""
        def mock_verify(provider):
            if provider == "gitcode":
                return {"healthy": True, "error": "", "error_type": ""}
            return {"healthy": False, "error": "401", "error_type": "auth"}
        with patch("src.runtime.bot_token_health._verify_bot_token", side_effect=mock_verify):
            from src.runtime.bot_token_health import _verify_global_bot_token
            result = _verify_global_bot_token()
        assert result["platforms"]["gitcode"]["healthy"] is True
        assert result["platforms"]["github"]["healthy"] is False
        assert result["platforms"]["github"]["error_type"] == "auth"

    def test_both_fail(self):
        """Both platforms fail → both in result with各自的 error_type."""
        def mock_verify(provider):
            if provider == "gitcode":
                return {"healthy": False, "error": "timeout", "error_type": "network"}
            return {"healthy": False, "error": "401", "error_type": "auth"}
        with patch("src.runtime.bot_token_health._verify_bot_token", side_effect=mock_verify):
            from src.runtime.bot_token_health import _verify_global_bot_token
            result = _verify_global_bot_token()
        assert result["platforms"]["gitcode"]["error_type"] == "network"
        assert result["platforms"]["github"]["error_type"] == "auth"

    def test_one_config(self):
        """GitCode healthy, GitHub not configured → both in result, config visible."""
        def mock_verify(provider):
            if provider == "gitcode":
                return {"healthy": True, "error": "", "error_type": ""}
            return {"healthy": False, "error": "GitHub token not configured", "error_type": "config"}
        with patch("src.runtime.bot_token_health._verify_bot_token", side_effect=mock_verify):
            from src.runtime.bot_token_health import _verify_global_bot_token
            result = _verify_global_bot_token()
        assert result["platforms"]["gitcode"]["healthy"] is True
        assert result["platforms"]["github"]["error_type"] == "config"


class TestUpdateHealthStatus:
    """Per-platform failure counting + WorstOf aggregation."""

    def test_first_check_both_healthy(self):
        """Both platforms healthy on first check → overall=healthy."""
        result = {"platforms": {
            "gitcode": {"healthy": True, "error": "", "error_type": ""},
            "github": {"healthy": True, "error": "", "error_type": ""},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=None), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        assert status["overall"] == "healthy"
        assert status["platforms"]["gitcode"]["consecutive_failures"] == 0
        assert status["platforms"]["github"]["consecutive_failures"] == 0

    def test_first_failure_unhealthy(self):
        """One platform auth-fails first time → failures=1 → unhealthy (R17: FAILURE_THRESHOLD=1)."""
        result = {"platforms": {
            "gitcode": {"healthy": True, "error": "", "error_type": ""},
            "github": {"healthy": False, "error": "401", "error_type": "auth"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=None), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        # R17: auth 401 首次失败 → failures=1 → github unhealthy
        # 但 gitcode healthy → overall=degraded（不是 unhealthy，因为不是所有平台都 unhealthy）
        assert status["overall"] == "degraded"
        assert status["platforms"]["github"]["consecutive_failures"] == 1
        assert status["platforms"]["github"]["unhealthy"] is True
        assert status["platforms"]["github"]["degraded"] is False
        assert status["platforms"]["gitcode"]["healthy"] is True

    def test_third_failure_unhealthy(self):
        """GitHub fails 3rd time (unhealthy), GitCode also fails 1st time (degraded) → overall=degraded."""
        prev = {"overall": "degraded", "platforms": {
            "gitcode": {"healthy": True, "consecutive_failures": 0, "degraded": False, "unhealthy": False,
                        "error_type": "", "error": ""},
            "github": {"healthy": False, "consecutive_failures": 2, "degraded": True, "unhealthy": False,
                       "error_type": "auth", "error": "401"},
        }}
        result = {"platforms": {
            "gitcode": {"healthy": False, "error": "timeout", "error_type": "network"},
            "github": {"healthy": False, "error": "401", "error_type": "auth"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=prev), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        assert status["platforms"]["github"]["consecutive_failures"] == 3
        assert status["platforms"]["github"]["unhealthy"] is True
        # GitCode is degraded (1st failure), GitHub is unhealthy → overall = degraded (not all unhealthy)
        assert status["overall"] == "degraded"

    def test_both_platforms_unhealthy(self):
        """Both platforms auth-fail → both unhealthy → overall=unhealthy."""
        prev = {"overall": "degraded", "platforms": {
            "gitcode": {"healthy": False, "consecutive_failures": 2, "degraded": False, "unhealthy": True,
                        "error_type": "auth", "error": "401"},
            "github": {"healthy": False, "consecutive_failures": 2, "degraded": False, "unhealthy": True,
                       "error_type": "auth", "error": "401"},
        }}
        result = {"platforms": {
            "gitcode": {"healthy": False, "error": "401", "error_type": "auth"},
            "github": {"healthy": False, "error": "401", "error_type": "auth"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=prev), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        # R17: auth 失败递增 failures，两个平台都 → failures=3 → unhealthy
        assert status["platforms"]["gitcode"]["consecutive_failures"] == 3
        assert status["platforms"]["gitcode"]["unhealthy"] is True
        assert status["platforms"]["github"]["consecutive_failures"] == 3
        assert status["platforms"]["github"]["unhealthy"] is True
        # Both unhealthy, no degraded → overall = unhealthy
        assert status["overall"] == "unhealthy"

    def test_one_platform_unhealthy_other_healthy(self):
        """GitHub unhealthy (3 failures), GitCode healthy → overall=degraded."""
        prev = {"overall": "degraded", "platforms": {
            "gitcode": {"healthy": True, "consecutive_failures": 0, "degraded": False, "unhealthy": False,
                        "error_type": "", "error": ""},
            "github": {"healthy": False, "consecutive_failures": 2, "degraded": True, "unhealthy": False,
                       "error_type": "auth", "error": "401"},
        }}
        result = {"platforms": {
            "gitcode": {"healthy": True, "error": "", "error_type": ""},
            "github": {"healthy": False, "error": "401", "error_type": "auth"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=prev), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        assert status["platforms"]["github"]["consecutive_failures"] == 3
        assert status["platforms"]["github"]["unhealthy"] is True
        assert status["platforms"]["gitcode"]["healthy"] is True
        # GitHub unhealthy but GitCode healthy → overall = degraded (not unhealthy)
        assert status["overall"] == "degraded"

    def test_recovery_resets_counter(self):
        """Platform recovers → consecutive_failures reset to 0."""
        prev = {"overall": "degraded", "platforms": {
            "gitcode": {"healthy": False, "consecutive_failures": 2, "degraded": True, "unhealthy": False,
                        "error_type": "auth", "error": "401"},
        }}
        result = {"platforms": {
            "gitcode": {"healthy": True, "error": "", "error_type": ""},
            "github": {"healthy": True, "error": "", "error_type": ""},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=prev), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        assert status["overall"] == "healthy"
        assert status["platforms"]["gitcode"]["consecutive_failures"] == 0

    def test_config_platform_skipped(self):
        """GitHub not configured → skipped, only GitCode in platforms, overall follows GitCode."""
        result = {"platforms": {
            "gitcode": {"healthy": True, "error": "", "error_type": ""},
            "github": {"healthy": False, "error": "GitHub token not configured", "error_type": "config"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=None), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        assert "github" not in status["platforms"]
        assert status["platforms"]["gitcode"]["healthy"] is True
        assert status["overall"] == "healthy"

    def test_config_platform_skipped_other_fails(self):
        """GitHub not configured, GitCode auth-fails → only GitCode counted, overall=unhealthy (R17: 1次401即unhealthy)."""
        result = {"platforms": {
            "gitcode": {"healthy": False, "error": "401", "error_type": "auth"},
            "github": {"healthy": False, "error": "GitHub token not configured", "error_type": "config"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=None), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        assert "github" not in status["platforms"]
        assert status["platforms"]["gitcode"]["consecutive_failures"] == 1
        # R17: auth 首次失败 → failures=1 → unhealthy（不是 degraded）
        assert status["overall"] == "unhealthy"

    def test_config_platform_skipped_other_reaches_unhealthy(self):
        """GitHub not configured, GitCode fails 3 times → overall=unhealthy (only configured platform failed)."""
        prev = {"overall": "degraded", "platforms": {
            "gitcode": {"healthy": False, "consecutive_failures": 2, "degraded": True, "unhealthy": False,
                        "error_type": "auth", "error": "401"},
        }}
        result = {"platforms": {
            "gitcode": {"healthy": False, "error": "401", "error_type": "auth"},
            "github": {"healthy": False, "error": "GitHub token not configured", "error_type": "config"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=prev), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        assert "github" not in status["platforms"]
        assert status["platforms"]["gitcode"]["consecutive_failures"] == 3
        assert status["platforms"]["gitcode"]["unhealthy"] is True
        # Only one configured platform, it's unhealthy → overall = unhealthy
        assert status["overall"] == "unhealthy"

    def test_both_config_skipped(self):
        """Both platforms not configured → platforms empty, overall=healthy."""
        result = {"platforms": {
            "gitcode": {"healthy": False, "error": "GitCode token not configured", "error_type": "config"},
            "github": {"healthy": False, "error": "GitHub token not configured", "error_type": "config"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=None), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        assert status["platforms"] == {}
        assert status["overall"] == "healthy"

    def test_independent_failure_counters(self):
        """GitCode fails 2nd time (auth), GitHub fails 1st time (auth) → independent counters."""
        prev = {"overall": "unhealthy", "platforms": {
            "gitcode": {"healthy": False, "consecutive_failures": 1, "degraded": False, "unhealthy": True,
                        "error_type": "auth", "error": "401"},
            "github": {"healthy": True, "consecutive_failures": 0, "degraded": False, "unhealthy": False,
                       "error_type": "", "error": ""},
        }}
        result = {"platforms": {
            "gitcode": {"healthy": False, "error": "401", "error_type": "auth"},
            "github": {"healthy": False, "error": "401", "error_type": "auth"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=prev), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        # R17: auth 失败递增 failures — gitcode 1→2, github 0→1
        assert status["platforms"]["gitcode"]["consecutive_failures"] == 2
        assert status["platforms"]["github"]["consecutive_failures"] == 1
        # 两个都是 auth 失败且 failures >= 1 → 都 unhealthy → overall=unhealthy
        assert status["platforms"]["gitcode"]["unhealthy"] is True
        assert status["platforms"]["github"]["unhealthy"] is True
        assert status["overall"] == "unhealthy"

    def test_mixed_error_types_per_platform(self):
        """Each platform keeps its own error_type."""
        result = {"platforms": {
            "gitcode": {"healthy": False, "error": "timeout", "error_type": "network"},
            "github": {"healthy": False, "error": "401", "error_type": "auth"},
        }}
        with patch("src.runtime.bot_token_health._get_health_status", return_value=None), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.runtime.bot_token_health import _update_health_status
            _update_health_status(result)
            status = json.loads(mock_set.call_args[0][1])
        assert status["platforms"]["gitcode"]["error_type"] == "network"
        assert status["platforms"]["github"]["error_type"] == "auth"


class TestHealthLoop:
    """探活主循环测试。"""

    @pytest.mark.asyncio
    async def test_loop_acquires_lock_and_checks(self):
        """正常路径：抢锁 → 探活 → 更新状态 → 释放锁。"""
        async def _mock_sleep(seconds):
            raise asyncio.CancelledError()

        verify_result = {"platforms": {
            "gitcode": {"healthy": True, "error": "", "error_type": ""},
            "github": {"healthy": True, "error": "", "error_type": ""},
        }}
        with patch("src.lock.acquire_lock", return_value=True) as mock_acquire, \
             patch("src.lock.release_lock") as mock_release, \
             patch("src.runtime.bot_token_health._verify_global_bot_token",
                   return_value=verify_result), \
             patch("src.runtime.bot_token_health._update_health_status") as mock_update, \
             patch("asyncio.sleep", side_effect=_mock_sleep):
            from src.runtime.bot_token_health import bot_token_health_loop
            with pytest.raises(asyncio.CancelledError):
                await bot_token_health_loop()

        # R17: 锁名改为 token_health_check，ttl=3000
        mock_acquire.assert_called_once_with("token_health_check", ttl=3000)
        mock_release.assert_called_once_with("token_health_check")
        mock_update.assert_called_once_with(verify_result)

    @pytest.mark.asyncio
    async def test_loop_skips_when_lock_not_acquired(self):
        """其他实例持锁时跳过探活。"""
        async def _mock_sleep(seconds):
            raise asyncio.CancelledError()

        with patch("src.lock.acquire_lock", return_value=False), \
             patch("src.lock.release_lock") as mock_release, \
             patch("src.runtime.bot_token_health._verify_global_bot_token") as mock_verify, \
             patch("asyncio.sleep", side_effect=_mock_sleep):
            from src.runtime.bot_token_health import bot_token_health_loop
            with pytest.raises(asyncio.CancelledError):
                await bot_token_health_loop()

        mock_verify.assert_not_called()
        mock_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_loop_lock_released_on_exception(self):
        """探活异常时锁也要释放。"""
        async def _mock_sleep(seconds):
            raise asyncio.CancelledError()

        with patch("src.lock.acquire_lock", return_value=True), \
             patch("src.lock.release_lock") as mock_release, \
             patch("src.runtime.bot_token_health._verify_global_bot_token",
                   side_effect=Exception("DB error")), \
             patch("asyncio.sleep", side_effect=_mock_sleep):
            from src.runtime.bot_token_health import bot_token_health_loop
            with pytest.raises(asyncio.CancelledError):
                await bot_token_health_loop()

        # R17: 锁名改为 token_health_check
        mock_release.assert_called_once_with("token_health_check")


class TestStartBotTokenHealth:
    """启动函数测试。"""

    @pytest.mark.asyncio
    async def test_idempotent_start(self):
        """已有同协程任务时不重复启动。"""
        from src.runtime.bot_token_health import start_bot_token_health, bot_token_health_loop
        loop = asyncio.get_running_loop()
        t1 = loop.create_task(bot_token_health_loop(), name="bot-token-health")
        try:
            t2 = start_bot_token_health()
            assert t2 is t1
        finally:
            t1.cancel()
            try:
                await t1
            except asyncio.CancelledError:
                pass
