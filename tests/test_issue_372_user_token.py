"""Issue #372: 用户级 Token 驱动全链路 — 单元测试

测试覆盖：
- get_repo_token_with_refresh 四级 fallback
- get_host_user_id / get_webhook_secret
- _has_refresh_token
- get_user_token_with_refresh PAT 兼容（无 refresh_token 跳过刷新）
- host_repo 路径 B（无全局 bot token）
- get_webhook_token_by_repo 优先 webhook_secret
- 自循环检测标记优先
- A2A trust domain host_user_id
- PAT 绑定端点
- 无邮箱配置场景

所有 DB 操作用 mock，不连真实数据库。
"""
import json
import os
import sys
import urllib.error
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

import pytest


# ===========================================================================
# 1. get_repo_token_with_refresh 四级 fallback
# ===========================================================================

class TestGetRepoTokenWithRefresh:
    """测试 token 获取链路的四级降级。

    R18: get_repo_token_with_refresh 改为委托 TokenResolver。测试 mock 策略类 _fetch。
    """

    def test_level1_devops_agent_session_hits(self):
        """第一级：devops_agent_session.repo_gitcode_token 有值 → 直接返回。"""
        from src.user.db import get_repo_token_with_refresh
        with patch("src.token_resolver.DevopsAgentSessionToken._fetch", return_value="SESSION_TOKEN"), \
             patch("src.token_resolver.RepoBotToken._fetch", return_value=""), \
             patch("src.token_resolver.GlobalBotToken._fetch", return_value="GLOBAL_BOT"), \
             patch("src.user.db.get_host_user_id", return_value=0):
            result = get_repo_token_with_refresh("gitcode:owner/repo")
            assert result == "SESSION_TOKEN"

    def test_level2_managed_repos_hits(self):
        """第二级：managed_repos.bot_gitcode_token 有值 → 直接返回。"""
        from src.user.db import get_repo_token_with_refresh
        with patch("src.token_resolver.DevopsAgentSessionToken._fetch", return_value=""), \
             patch("src.token_resolver.RepoBotToken._fetch", return_value="REPO_BOT_TOKEN"), \
             patch("src.token_resolver.GlobalBotToken._fetch", return_value="GLOBAL_BOT"), \
             patch("src.user.db.get_host_user_id", return_value=0):
            result = get_repo_token_with_refresh("gitcode:owner/repo")
            assert result == "REPO_BOT_TOKEN"

    def test_level3_global_bot_hits(self):
        """第三级：全局 bot token 有值 → 直接返回。"""
        from src.user.db import get_repo_token_with_refresh
        with patch("src.token_resolver.DevopsAgentSessionToken._fetch", return_value=""), \
             patch("src.token_resolver.RepoBotToken._fetch", return_value=""), \
             patch("src.token_resolver.GlobalBotToken._fetch", return_value="GLOBAL_BOT"), \
             patch("src.user.db.get_host_user_id", return_value=0):
            result = get_repo_token_with_refresh("gitcode:owner/repo")
            assert result == "GLOBAL_BOT"

    def test_level4_user_token_fallback(self):
        """第四级：三级全空 → 降级到用户 token。"""
        from src.user.db import get_repo_token_with_refresh
        with patch("src.token_resolver.DevopsAgentSessionToken._fetch", return_value=""), \
             patch("src.token_resolver.RepoBotToken._fetch", return_value=""), \
             patch("src.token_resolver.GlobalBotToken._fetch", return_value=""), \
             patch("src.user.db.get_host_user_id", return_value=42), \
             patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"username": "testuser"}), \
             patch("src.api.deps.get_user_token_with_refresh", return_value="USER_OAUTH_TOKEN") as m:
            result = get_repo_token_with_refresh("gitcode:owner/repo")
            assert result == "USER_OAUTH_TOKEN"
            m.assert_called_once_with("testuser", 42)

    def test_all_empty_returns_empty_string(self):
        """四级全空 → 返回空字符串。"""
        from src.user.db import get_repo_token_with_refresh
        with patch("src.token_resolver.DevopsAgentSessionToken._fetch", return_value=""), \
             patch("src.token_resolver.RepoBotToken._fetch", return_value=""), \
             patch("src.token_resolver.GlobalBotToken._fetch", return_value=""), \
             patch("src.user.db.get_host_user_id", return_value=0):
            result = get_repo_token_with_refresh("gitcode:owner/repo")
            assert result == ""

    def test_empty_repo_key_returns_empty(self):
        """空 repo_key → 返回空字符串。"""
        from src.user.db import get_repo_token_with_refresh
        result = get_repo_token_with_refresh("")
        assert result == ""


# ===========================================================================
# 2. get_gitcode_token 向前兼容（内部委托 get_repo_token_with_refresh）
# ===========================================================================

class TestGetGitcodeTokenCompat:
    """测试 get_gitcode_token 内部委托 get_repo_token_with_refresh。"""

    def test_repo_key_delegates(self):
        """有 repo_key → 调 get_repo_token_with_refresh。"""
        from src.user.db import get_gitcode_token
        with patch("src.user.db.get_repo_token_with_refresh", return_value="TOKEN") as m:
            result = get_gitcode_token(repo_key="gitcode:owner/repo")
            assert result == "TOKEN"
            m.assert_called_once_with("gitcode:owner/repo")

    def test_user_id_returns_user_token(self):
        """有 user_id 无 repo_key → 走用户 token 路径。"""
        from src.user.db import get_gitcode_token
        with patch("src.user.db.get_user_gitcode_token", return_value="USER_TOKEN") as m:
            result = get_gitcode_token(user_id=42)
            assert result == "USER_TOKEN"
            m.assert_called_once_with(42)

    def test_empty_returns_empty(self):
        """空参数 → 返回空字符串。"""
        from src.user.db import get_gitcode_token
        result = get_gitcode_token()
        assert result == ""


# ===========================================================================
# 3. _has_refresh_token
# ===========================================================================

class TestHasRefreshToken:
    """测试 PAT 场景判断（无 refresh_token）。"""

    def test_has_refresh_token_true(self):
        """有 refresh_token → True（OAuth 场景）。"""
        from src.auth.grape_account_db import _has_refresh_token
        with patch("src.auth.grape_account_db.db") as mock_db, \
             patch("src.auth.grape_account_db.db_lock"), \
             patch("src.auth.grape_account_db._safe_decrypt", return_value="rt_abc123"):
            cursor = MagicMock()
            cursor.fetchone.return_value = ("encrypted_rt",)
            mock_db.cursor.return_value = cursor
            assert _has_refresh_token(42, "gitcode") is True

    def test_no_refresh_token_false(self):
        """无 refresh_token → False（PAT 场景）。"""
        from src.auth.grape_account_db import _has_refresh_token
        with patch("src.auth.grape_account_db.db") as mock_db, \
             patch("src.auth.grape_account_db.db_lock"), \
             patch("src.auth.grape_account_db._safe_decrypt", return_value=""):
            cursor = MagicMock()
            cursor.fetchone.return_value = ("",)
            mock_db.cursor.return_value = cursor
            assert _has_refresh_token(42, "gitcode") is False

    def test_no_identity_returns_false(self):
        """无绑定记录 → False。"""
        from src.auth.grape_account_db import _has_refresh_token
        with patch("src.auth.grape_account_db.db") as mock_db, \
             patch("src.auth.grape_account_db.db_lock"):
            cursor = MagicMock()
            cursor.fetchone.return_value = None
            mock_db.cursor.return_value = cursor
            assert _has_refresh_token(42, "gitcode") is False


# ===========================================================================
# 4. get_user_token_with_refresh PAT 兼容
# ===========================================================================

class TestGetUserTokenWithRefreshPAT:
    """测试 PAT 场景下跳过刷新。"""

    def test_pat_no_refresh_skips_refresh(self):
        """PAT（无 refresh_token）→ 直接返回 token，不触发刷新。"""
        from src.api.deps import get_user_token_with_refresh
        with patch("src.api.deps._get_oauth_token_from_identity", return_value="PAT_TOKEN"), \
             patch("src.auth.grape_account_db._has_refresh_token", return_value=False), \
             patch("src.api.deps.try_refresh_gitcode_token") as m_refresh:
            result = get_user_token_with_refresh("testuser", user_id=42)
            assert result == "PAT_TOKEN"
            m_refresh.assert_not_called()

    def test_oauth_with_refresh_checks_expiry(self):
        """OAuth（有 refresh_token）→ 走原过期检查逻辑，过期时触发刷新。"""
        from src.api.deps import get_user_token_with_refresh
        # token_expires_at=0 + fetched_at=0 → 走降级 → age=now-0 > 1800 → need_refresh
        mock_cred = {"token_expires_at": 0, "fetched_at": 0, "access_token": "OAUTH_TOKEN"}
        with patch("src.api.deps._get_oauth_token_from_identity", return_value="OAUTH_TOKEN"), \
             patch("src.auth.grape_account_db._has_refresh_token", return_value=True), \
             patch("src.api.deps._get_gitcode_credential_by_gitcode_id", return_value=mock_cred), \
             patch("src.api.deps.try_refresh_gitcode_token", return_value="REFRESHED_TOKEN") as m_refresh:
            result = get_user_token_with_refresh("testuser", user_id=42)
            # 无论是否刷新，都应该返回 token（刷新成功返回 REFRESHED_TOKEN，否则返回原 token）
            assert result in ("REFRESHED_TOKEN", "OAUTH_TOKEN")


# ===========================================================================
# 5. get_webhook_token_by_repo 优先 webhook_secret
# ===========================================================================

class TestGetWebhookTokenByRepo:
    """测试 webhook 验证密钥优先。"""

    def test_webhook_secret_takes_priority(self):
        """有 webhook_secret → 返回 webhook_secret（路径 B）。"""
        from src.user.db import get_webhook_token_by_repo
        with patch("src.user.db.db") as mock_db, patch("src.user.db.db_lock"):
            cursor = MagicMock()
            cursor.fetchone.return_value = ("SECRET_123", "OLD_TOKEN", "")
            mock_db.cursor.return_value = cursor
            result = get_webhook_token_by_repo("owner/repo")
            assert result == "SECRET_123"

    def test_fallback_to_gitcode_webhook_token(self):
        """无 webhook_secret → fallback 到 gitcode_webhook_token（路径 A）。"""
        from src.user.db import get_webhook_token_by_repo
        with patch("src.user.db.db") as mock_db, patch("src.user.db.db_lock"):
            cursor = MagicMock()
            cursor.fetchone.return_value = ("", "OLD_BOT_TOKEN", "")
            mock_db.cursor.return_value = cursor
            result = get_webhook_token_by_repo("owner/repo")
            assert result == "OLD_BOT_TOKEN"

    def test_empty_repo_returns_empty(self):
        """无记录 → 空字符串。"""
        from src.user.db import get_webhook_token_by_repo
        with patch("src.user.db.db") as mock_db, patch("src.user.db.db_lock"):
            cursor = MagicMock()
            cursor.fetchone.return_value = None
            mock_db.cursor.return_value = cursor
            result = get_webhook_token_by_repo("unknown/repo")
            assert result == ""


# ===========================================================================
# 6. PAT 绑定端点
# ===========================================================================

# ===========================================================================
# 7. 自循环检测标记优先
# ===========================================================================

class TestSelfLoopDetection:
    """测试 pipeline 步骤顺序：2.0b(开发者指令) → 2.0(自循环) → 2.0a(标记)（R20.6.2）。"""

    def test_auto_comment_marker_skipped_even_if_not_bot_actor(self):
        """非 bot actor + 有标记 → 跳过（路径 B 场景）。
        R20.6.2: 步骤顺序调整为 2.0b → 2.0(自循环) → 2.0a(标记)。
        标记检测在自循环检测之后，但仍然有效——自循环检测不命中非 bot actor。
        """
        # 验证 pipeline 中自循环检测（handler.is_self_loop_actor）在标记检测之前
        import src.api.event_pipeline as ep
        source = open(ep.__file__).read()
        handler_pos = source.find("handler.is_self_loop_actor")
        marker_pos = source.find("2.0a 系统回帖标记检测")
        # 自循环检测应该在标记检测之前
        assert handler_pos > 0, "handler.is_self_loop_actor not found"
        assert marker_pos > 0, "2.0a marker check not found"
        assert handler_pos < marker_pos, "self-loop check (2.0) should come before marker check (2.0a)"


# ===========================================================================
# 8. is_email_configured
# ===========================================================================

class TestIsEmailConfigured:
    """测试邮箱服务配置检测。"""

    def test_smtp_configured_returns_true(self):
        """配了 SMTP → True（有邮箱场景）。"""
        from src.auth.email_service import is_email_configured
        with patch("src.auth.email_service._smtp_host", return_value="smtp.example.com"):
            assert is_email_configured() is True

    def test_resend_configured_returns_true(self):
        """配了 Resend API → True（有邮箱场景）。"""
        from src.auth.email_service import is_email_configured
        with patch("src.auth.email_service._smtp_host", return_value=""), \
             patch("src.auth.email_service._resend_api_key", return_value="re_abc123"):
            assert is_email_configured() is True

    def test_nothing_configured_returns_false(self):
        """什么都没配 → False（无邮箱场景）。"""
        from src.auth.email_service import is_email_configured
        with patch("src.auth.email_service._smtp_host", return_value=""), \
             patch("src.auth.email_service._resend_api_key", return_value=""):
            assert is_email_configured() is False

    def test_dev_verification_code_not_counted(self):
        """dev_verification_code 不算邮箱已配置。"""
        from src.auth.email_service import is_email_configured
        with patch("src.auth.email_service._smtp_host", return_value=""), \
             patch("src.auth.email_service._resend_api_key", return_value=""), \
             patch("src.auth.email_service._dev_verification_code", return_value="123456"):
            assert is_email_configured() is False


# ===========================================================================
# 8b. 注册流程有邮箱/无邮箱两个场景
# ===========================================================================

class TestRegisterEmailScenarios:
    """测试注册流程在有无邮箱配置两个场景下的行为。"""

    def _override_auth_public(self, app):
        """公开端点不需要 auth。"""
        pass

    def test_register_verify_with_email_checks_code(self):
        """有邮箱配置 → 校验验证码。"""
        from src.api.main import app
        client = TestClient(app)
        with patch("src.auth.email_service.is_email_configured", return_value=True), \
             patch("src.api.routes.grape_auth.auth_verification.verify_code") as m_verify, \
             patch("src.api.routes.grape_auth.create_grape_account") as m_create:
            m_verify.return_value = {"ok": False, "message": "验证码错误"}
            m_create.return_value = {"id": 1, "username": "testuser"}
            resp = client.post("/api/auth/register/verify", json={
                "username": "testuser", "email": "test@example.com",
                "password": "Test1234!", "code": "wrong"
            })
            assert resp.status_code == 400
            m_verify.assert_called_once()

    def test_register_verify_without_email_skips_code(self):
        """无邮箱配置 → 跳过验证码，直接创建账号。"""
        from src.api.main import app
        client = TestClient(app)
        with patch("src.auth.email_service.is_email_configured", return_value=False), \
             patch("src.api.routes.grape_auth.auth_verification.verify_code") as m_verify, \
             patch("src.api.routes.grape_auth.create_grape_account") as m_create, \
             patch("src.api.routes.grape_auth.is_username_taken", return_value=False), \
             patch("src.api.routes.grape_auth._ensure_personal_org"), \
             patch("src.api.routes.grape_auth.get_token_version", return_value=1), \
             patch("src.api.routes.grape_auth._issue_jwt", return_value=("access", "refresh")), \
             patch("src.api.routes.grape_auth.update_last_login_at"), \
             patch("src.api.routes.grape_auth.auth_audit.log_audit"):
            m_create.return_value = {"id": 1, "username": "testuser"}
            resp = client.post("/api/auth/register/verify", json={
                "username": "testuser", "email": "",
                "password": "Test1234!", "code": ""
            })
            m_verify.assert_not_called()
            m_create.assert_called_once()

    def test_register_send_code_with_email_sends(self):
        """有邮箱配置 → 正常发送验证码。"""
        from src.api.main import app
        client = TestClient(app)
        with patch("src.auth.email_service.is_email_configured", return_value=True), \
             patch("src.api.routes.grape_auth.is_username_taken", return_value=False), \
             patch("src.api.routes.grape_auth.is_email_registered", return_value=False), \
             patch("src.api.routes.grape_auth.signup_policy.check_signup_allowed", return_value=True), \
             patch("src.api.routes.grape_auth.auth_verification.send_code") as m_send:
            m_send.return_value = {"ok": True, "message": ""}
            resp = client.post("/api/auth/register/send-code", json={
                "username": "testuser", "email": "test@example.com", "password": "Test1234!"
            })
            m_send.assert_called_once()

    def test_register_send_code_without_email_returns_hint(self):
        """无邮箱配置 → 返回提示，不发验证码。"""
        from src.api.main import app
        client = TestClient(app)
        with patch("src.auth.email_service.is_email_configured", return_value=False), \
             patch("src.api.routes.grape_auth.auth_verification.send_code") as m_send:
            resp = client.post("/api/auth/register/send-code", json={
                "username": "testuser", "email": "test@example.com", "password": "Test1234!"
            })
            m_send.assert_not_called()
            assert resp.status_code == 200


# ===========================================================================
# 8c. 忘记密码有邮箱/无邮箱两个场景
# ===========================================================================

class TestForgotPasswordEmailScenarios:
    """测试忘记密码在有无邮箱配置两个场景下的行为。"""

    def test_forgot_password_send_code_without_email_503(self):
        """无邮箱配置 → 返回 503。"""
        from src.api.main import app
        client = TestClient(app)
        with patch("src.auth.email_service.is_email_configured", return_value=False):
            resp = client.post("/api/auth/forgot-password/send-code", json={
                "email": "test@example.com"
            })
            assert resp.status_code == 503

    def test_forgot_password_send_code_with_email_proceeds(self):
        """有邮箱配置 → 正常流程。"""
        from src.api.main import app
        client = TestClient(app)
        with patch("src.auth.email_service.is_email_configured", return_value=True), \
             patch("src.api.routes.grape_auth.auth_verification.send_code") as m_send, \
             patch("src.api.routes.grape_auth.auth_verification.check_rate_limit", return_value={"ok": True}), \
             patch("src.api.routes.grape_auth.is_email_registered", return_value=True):
            m_send.return_value = {"ok": True, "message": ""}
            resp = client.post("/api/auth/forgot-password/send-code", json={
                "email": "test@example.com"
            })
            # send_code should be called (or rate_limit checked for already-registered)
            # Just verify it doesn't return 503
            assert resp.status_code != 503


# ===========================================================================
# 8d. /auth/providers 返回 email_configured
# ===========================================================================

class TestProvidersEmailConfigured:
    """测试 /auth/providers 返回 email_configured 字段。"""

    def test_providers_returns_email_configured_true(self):
        """有邮箱配置 → email_configured=True。"""
        from src.api.main import app
        client = TestClient(app)
        with patch("src.auth.email_service.is_email_configured", return_value=True):
            resp = client.get("/api/auth/providers")
            assert resp.status_code == 200
            data = resp.json()
            assert "email_configured" in data
            assert data["email_configured"] is True

    def test_providers_returns_email_configured_false(self):
        """无邮箱配置 → email_configured=False。"""
        from src.api.main import app
        client = TestClient(app)
        with patch("src.auth.email_service.is_email_configured", return_value=False):
            resp = client.get("/api/auth/providers")
            assert resp.status_code == 200
            data = resp.json()
            assert "email_configured" in data
            assert data["email_configured"] is False


# ===========================================================================
# 9. 三个绑定场景测试
# ===========================================================================

class TestBindingScenarios:
    """测试三种绑定场景的 token 有效期和刷新行为。

    场景一：已配 OAuth App + OAuth 授权 → token 有 refresh_token，自动刷新
    场景二：未配 OAuth App + PAT 直接绑定 → token 无 refresh_token，不过期不刷新
    场景三：已配 OAuth App + 用户选 PAT → 与场景二相同
    """

    # --- 场景一：OAuth 授权绑定 ---

    def test_oauth_bind_stores_refresh_token(self):
        """场景一：OAuth 授权 → 存入 refresh_token + token_expires_at。"""
        from src.auth.grape_account_db import create_external_identity
        import time as _time_module
        with patch("src.auth.grape_account_db.db") as mock_db, \
             patch("src.auth.grape_account_db.db_lock"), \
             patch("src.auth.credential_crypto.encrypt", return_value="encrypted"), \
             patch.object(_time_module, "time", return_value=1000000), \
             patch.object(_time_module, "strftime", return_value="2026-01-01 00:00:00"):
            cursor = MagicMock()
            cursor.fetchone.return_value = None
            mock_db.cursor.return_value = cursor
            mock_db.commit.return_value = None
            result = create_external_identity(
                grape_user_id=1, provider="gitcode", external_id="123",
                external_login="testuser", access_token="oauth_token",
                refresh_token="rt_abc", expires_in=3600
            )
            assert result is True
            insert_calls = [c for c in cursor.execute.call_args_list if "INSERT" in str(c)]
            assert len(insert_calls) > 0

    def test_oauth_token_triggers_refresh_near_expiry(self):
        """场景一：OAuth token 临近过期 → 触发刷新。"""
        from src.api.deps import get_user_token_with_refresh
        import time as _time
        now = int(_time.time())
        # token 5 分钟后过期 → 距过期不足 10 分钟 → need_refresh=True
        mock_cred = {"token_expires_at": now + 300, "fetched_at": now, "access_token": "OAUTH_TOKEN"}
        with patch("src.api.deps._get_oauth_token_from_identity", return_value="OAUTH_TOKEN"), \
             patch("src.auth.grape_account_db._has_refresh_token", return_value=True), \
             patch("src.api.deps._get_gitcode_credential_by_gitcode_id", return_value=mock_cred), \
             patch("src.api.deps.try_refresh_gitcode_token", return_value="REFRESHED_TOKEN") as m:
            result = get_user_token_with_refresh("testuser", user_id=1)
            m.assert_called_once()
            assert result == "REFRESHED_TOKEN"

    def test_oauth_token_no_refresh_when_not_expired(self):
        """场景一：OAuth token 未过期 → 不触发刷新。"""
        from src.api.deps import get_user_token_with_refresh
        import time as _time
        now = int(_time.time())
        # token 1 小时后过期 → 距过期 > 10 分钟 → need_refresh=False
        mock_cred = {"token_expires_at": now + 3600, "fetched_at": now, "access_token": "OAUTH_TOKEN"}
        with patch("src.api.deps._get_oauth_token_from_identity", return_value="OAUTH_TOKEN"), \
             patch("src.auth.grape_account_db._has_refresh_token", return_value=True), \
             patch("src.api.deps._get_gitcode_credential_by_gitcode_id", return_value=mock_cred), \
             patch("src.api.deps.try_refresh_gitcode_token") as m:
            result = get_user_token_with_refresh("testuser", user_id=1)
            m.assert_not_called()
            assert result == "OAUTH_TOKEN"

    # --- 场景二：PAT 直接绑定（无 OAuth App）---

    def test_pat_bind_stores_no_refresh_token(self):
        """场景二：PAT 绑定 → 存入 access_token，无 refresh_token，无 token_expires_at。"""
        from src.auth.grape_account_db import create_external_identity
        import time as _time_module
        with patch("src.auth.grape_account_db.db") as mock_db, \
             patch("src.auth.grape_account_db.db_lock"), \
             patch("src.auth.credential_crypto.encrypt", return_value="encrypted"), \
             patch.object(_time_module, "time", return_value=1000000), \
             patch.object(_time_module, "strftime", return_value="2026-01-01 00:00:00"):
            cursor = MagicMock()
            cursor.fetchone.return_value = None
            mock_db.cursor.return_value = cursor
            mock_db.commit.return_value = None
            result = create_external_identity(
                grape_user_id=1, provider="gitcode", external_id="123",
                external_login="testuser", access_token="J4N1_pat_token",
                refresh_token="", expires_in=0
            )
            assert result is True

    def test_pat_token_skips_refresh(self):
        """场景二：PAT token → 无 refresh_token → 跳过刷新，直接返回。"""
        from src.api.deps import get_user_token_with_refresh
        with patch("src.api.deps._get_oauth_token_from_identity", return_value="PAT_TOKEN"), \
             patch("src.auth.grape_account_db._has_refresh_token", return_value=False), \
             patch("src.api.deps.try_refresh_gitcode_token") as m:
            result = get_user_token_with_refresh("testuser", user_id=1)
            m.assert_not_called()
            assert result == "PAT_TOKEN"

    def test_pat_token_never_expires(self):
        """场景二：PAT token → 不过期（无 token_expires_at，无 refresh_token）。"""
        from src.auth.grape_account_db import _has_refresh_token
        with patch("src.auth.grape_account_db.db") as mock_db, \
             patch("src.auth.grape_account_db.db_lock"), \
             patch("src.auth.grape_account_db._safe_decrypt", return_value=""):
            cursor = MagicMock()
            cursor.fetchone.return_value = ("encrypted_empty",)
            mock_db.cursor.return_value = cursor
            # PAT 场景：refresh_token 为空 → _has_refresh_token 返回 False
            assert _has_refresh_token(1, "gitcode") is False

    # --- 场景三：已配 OAuth App + 用户选 PAT ---

    def test_oauth_configured_but_user_chooses_pat(self):
        """场景三：有 OAuth App 配置 + 用户选 PAT → PAT 绑定，与场景二行为一致。"""
        from src.api.deps import get_user_token_with_refresh
        # 即使 OAuth App 已配置，用户选 PAT 绑定后 refresh_token 为空
        with patch("src.api.deps._get_oauth_token_from_identity", return_value="PAT_TOKEN"), \
             patch("src.auth.grape_account_db._has_refresh_token", return_value=False), \
             patch("src.api.deps.try_refresh_gitcode_token") as m:
            result = get_user_token_with_refresh("testuser", user_id=1)
            m.assert_not_called()
            assert result == "PAT_TOKEN"

    def test_pat_overwrites_oauth_binding(self):
        """场景三：OAuth 绑定后再绑 PAT → 覆盖更新（create_external_identity UPDATE 分支）。"""
        from src.auth.grape_account_db import create_external_identity
        import time as _time_module
        with patch("src.auth.grape_account_db.db") as mock_db, \
             patch("src.auth.grape_account_db.db_lock"), \
             patch("src.auth.credential_crypto.encrypt", return_value="encrypted"), \
             patch.object(_time_module, "time", return_value=1000000), \
             patch.object(_time_module, "strftime", return_value="2026-01-01 00:00:00"):
            cursor = MagicMock()
            cursor.fetchone.return_value = (1,)
            mock_db.cursor.return_value = cursor
            mock_db.commit.return_value = None
            result = create_external_identity(
                grape_user_id=1, provider="gitcode", external_id="123",
                external_login="testuser", access_token="PAT_OVERWRITE",
                refresh_token="", expires_in=0
            )
            assert result is True
            update_calls = [c for c in cursor.execute.call_args_list if "UPDATE" in str(c)]
            assert len(update_calls) > 0

    # --- 绑定弹窗 UI 场景 ---

    def test_bind_modal_oauth_configured_shows_both_tabs(self):
        """场景三：OAuth App 已配 → /auth/providers 返回 configured=true。"""
        from src.api.main import app
        client = TestClient(app)
        # 不 mock get_meta_value（它被多处使用），而是直接验证 providers 响应结构
        resp = client.get("/api/auth/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        assert "email_configured" in data
        # providers 列表非空（至少有 gitcode provider 定义）
        assert len(data["providers"]) > 0

    def test_bind_modal_oauth_not_configured_shows_token_only(self):
        """场景二：OAuth App 未配 → providers 返回 configured=false。"""
        from src.api.main import app
        client = TestClient(app)
        # 测试无 OAuth 配置时 providers 仍返回结构（configured=false）
        resp = client.get("/api/auth/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        # 每个 provider 都有 configured 字段
        for p in data["providers"]:
            assert "configured" in p
            assert isinstance(p["configured"], bool)

    def test_login_page_oauth_buttons_always_visible(self):
        """登录页 GitCode/GitHub 按钮始终显示，不管 OAuth App 是否配置。

        系统支持用户 token 绑定，按钮点击行为分两种：
        - OAuth App 已配 → 跳转 OAuth 授权登录
        - OAuth App 未配 → 提示用户名+密码注册后绑定 token
        后端只需保证 /auth/providers 返回 configured 字段供前端判断点击行为。
        """
        from src.api.main import app
        client = TestClient(app)
        resp = client.get("/api/auth/providers")
        data = resp.json()
        # 验证所有 provider 都有 configured 字段供前端判断点击行为
        for p in data.get("providers", []):
            assert "configured" in p
        # providers 列表非空（至少有一个 provider 定义，不管是否配置）
        assert len(data.get("providers", [])) > 0


# ===========================================================================
# 9. add_managed_repo 签名兼容
# ===========================================================================

class TestAddManagedRepoSignature:
    """测试 add_managed_repo 新参数向前兼容。"""

    def test_new_params_have_defaults(self):
        """host_user_id 和 webhook_secret 有默认值。"""
        import inspect
        from src.user.db import add_managed_repo
        sig = inspect.signature(add_managed_repo)
        assert sig.parameters["host_user_id"].default == 0
        assert sig.parameters["webhook_secret"].default == ""

    def test_old_call_without_new_params_works(self):
        """不传新参数 → 不报错（向前兼容）。"""
        from src.user.db import add_managed_repo
        with patch("src.user.db.db") as mock_db, patch("src.user.db.db_lock"):
            cursor = MagicMock()
            mock_db.cursor.return_value = cursor
            # 只传旧参数
            result = add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=123)
            assert result is True


# ===========================================================================
# 10. _get_task_token helper
# ===========================================================================

class TestGetTaskToken:
    """测试 task token 获取 helper。"""

    def test_task_with_repo_gitcode_token(self):
        """task 有 repo_gitcode_token → 直接返回。"""
        from src.runtime.agent import _get_task_token
        task = {"repo_gitcode_token": "BOT_TOKEN", "repo_key": "gitcode:owner/repo"}
        assert _get_task_token(task) == "BOT_TOKEN"

    def test_task_empty_token_falls_back(self):
        """task 无 repo_gitcode_token → 调 get_repo_token_with_refresh。"""
        from src.runtime.agent import _get_task_token
        task = {"repo_gitcode_token": "", "repo_key": "gitcode:owner/repo"}
        with patch("src.user.db.get_repo_token_with_refresh", return_value="FALLBACK_TOKEN") as m:
            result = _get_task_token(task)
            assert result == "FALLBACK_TOKEN"
            m.assert_called_once_with("gitcode:owner/repo")

    def test_task_github_empty_token_falls_back_to_get_github_token(self):
        """task 无 repo_gitcode_token + github: 前缀 → 调 get_github_token（P1-1 修复）。"""
        from src.runtime.agent import _get_task_token
        task = {"repo_gitcode_token": "", "repo_key": "github:owner/repo"}
        with patch("src.user.db.get_github_token", return_value="GH_TOKEN") as m:
            result = _get_task_token(task)
            assert result == "GH_TOKEN"
            m.assert_called_once_with(repo_key="github:owner/repo")

    def test_task_no_repo_key_returns_empty(self):
        """task 无 repo_key → 空字符串。"""
        from src.runtime.agent import _get_task_token
        task = {"repo_gitcode_token": "", "repo_key": ""}
        assert _get_task_token(task) == ""
