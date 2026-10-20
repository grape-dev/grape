"""R7: 无邮箱配置场景适配测试。

测试覆盖：
1. is_email_configured() 函数逻辑
2. /auth/providers 返回 email_configured 字段
3. 无邮箱配置时注册流程降级：
   - /auth/register/send-code 返回 200 + 提示消息
   - /auth/register/verify 跳过验证码校验，email 为空也能创建账号
4. 无邮箱配置时忘记密码流程降级：
   - /auth/forgot-password/send-code 返回 503

测试用 mock，不连真实 DB。
"""
import os
import sys
import uuid
from unittest.mock import patch

import pytest

# 强制使用 SQLite 测试库
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
os.environ["DEVOPS_DB_PATH"] = "/tmp/test_r7_no_email.db"

# 清理旧测试库
if os.path.exists("/tmp/test_r7_no_email.db"):
    os.remove("/tmp/test_r7_no_email.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.api.db_setup import _create_tables
from src.auth.grape_account_db import (
    is_username_taken, is_email_registered,
)
from src.auth.email_service import is_email_configured, _smtp_host, _resend_api_key

# 初始化表
_create_tables()


# D46: 全局 mock 邮件服务
@pytest.fixture(autouse=True)
def _mock_email_service():
    """mock 邮件服务，不依赖环境/配置/网络。"""
    with patch("src.auth.email_service.EmailService.send_verification_code", return_value=True), \
         patch("src.auth.email_service.EmailService.send_notification", return_value=True), \
         patch("src.auth.email_service.EmailService._send_smtp", return_value=True), \
         patch("src.auth.email_service.EmailService._send_resend", return_value=True):
        yield


_TEST_DEV_CODE = "888888"


@pytest.fixture(autouse=True)
def _mock_dev_code():
    """mock is_dev_verification_code，测试环境固定验证码 888888 直接通过。"""
    with patch("src.auth.verification.is_dev_verification_code", side_effect=lambda code: code == _TEST_DEV_CODE), \
         patch("src.auth.email_service.is_dev_verification_code", side_effect=lambda code: code == _TEST_DEV_CODE):
        yield


client = TestClient(app)


def _cleanup_user(uid: int):
    """清理测试用户。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
            c.execute("DELETE FROM verification_codes WHERE email LIKE 'test_%@example.com'")
            c.execute("DELETE FROM login_failures WHERE identifier LIKE 'test_%'")
            c.execute("DELETE FROM audit_logs WHERE user_id=?", (uid,))
            db.commit()
    except Exception:
        pass


# ===========================================================================
# 1. is_email_configured() 单元测试
# ===========================================================================

class TestIsEmailConfigured:
    """测试 is_email_configured() 函数逻辑。"""

    def test_returns_true_when_smtp_configured(self):
        """有 SMTP host 配置时返回 True。"""
        with patch("src.auth.email_service._smtp_host", return_value="smtp.example.com"), \
             patch("src.auth.email_service._resend_api_key", return_value=""):
            assert is_email_configured() is True

    def test_returns_true_when_resend_configured(self):
        """有 Resend API key 配置时返回 True。"""
        with patch("src.auth.email_service._smtp_host", return_value=""), \
             patch("src.auth.email_service._resend_api_key", return_value="re_abc123"):
            assert is_email_configured() is True

    def test_returns_true_when_both_configured(self):
        """SMTP 和 Resend 都配置时返回 True。"""
        with patch("src.auth.email_service._smtp_host", return_value="smtp.example.com"), \
             patch("src.auth.email_service._resend_api_key", return_value="re_abc123"):
            assert is_email_configured() is True

    def test_returns_false_when_neither_configured(self):
        """SMTP 和 Resend 都未配置时返回 False。"""
        with patch("src.auth.email_service._smtp_host", return_value=""), \
             patch("src.auth.email_service._resend_api_key", return_value=""):
            assert is_email_configured() is False

    def test_returns_false_even_in_non_production_with_dev_code(self):
        """非生产环境仅有 dev_verification_code 但无 SMTP/Resend 时返回 False。

        dev_verification_code 是开发环境固定验证码，不是邮件服务。
        """
        with patch("src.auth.email_service._smtp_host", return_value=""), \
             patch("src.auth.email_service._resend_api_key", return_value=""), \
             patch("src.auth.email_service._dev_verification_code", return_value="123456"), \
             patch("src.auth.email_service.is_production_env", return_value=False):
            assert is_email_configured() is False


# ===========================================================================
# 2. /auth/providers 返回 email_configured 字段
# ===========================================================================

class TestProvidersWithEmailConfig:
    """测试 /auth/providers 返回 email_configured 字段。"""

    def test_providers_returns_email_configured_true(self):
        """邮箱已配置时 email_configured 为 True。"""
        with patch("src.auth.email_service.is_email_configured", return_value=True):
            r = client.get("/api/auth/providers")
            assert r.status_code == 200
            data = r.json()
            assert "email_configured" in data
            assert data["email_configured"] is True

    def test_providers_returns_email_configured_false(self):
        """邮箱未配置时 email_configured 为 False。"""
        with patch("src.auth.email_service.is_email_configured", return_value=False):
            r = client.get("/api/auth/providers")
            assert r.status_code == 200
            data = r.json()
            assert "email_configured" in data
            assert data["email_configured"] is False


# ===========================================================================
# 3. 无邮箱配置时注册流程降级
# ===========================================================================

class TestRegisterNoEmailFallback:
    """测试无邮箱配置时注册流程降级。"""

    def test_register_send_code_returns_message_when_no_email(self):
        """无邮箱配置时 send-code 返回 200 + 提示消息。"""
        with patch("src.auth.email_service.is_email_configured", return_value=False):
            username = "test_noe_" + uuid.uuid4().hex[:6]
            r = client.post("/api/auth/register/send-code", json={
                "username": username, "email": f"{username}@example.com", "password": "TestPass123!"
            })
            assert r.status_code == 200
            assert r.json().get("error_code") == "EMAIL_NOT_CONFIGURED"
            assert "not configured" in r.json()["message"].lower()

    def test_register_verify_skips_code_check_when_no_email(self):
        """无邮箱配置时 verify 跳过验证码校验，email 可为空，直接创建账号。"""
        with patch("src.auth.email_service.is_email_configured", return_value=False):
            from src.user.db import set_meta_value
            set_meta_value("allow_signup", "1")
            try:
                username = "test_noe_verify_" + uuid.uuid4().hex[:6]
                password = "TestPass123!"
                # email 为空，code 为空——无邮箱配置时应直接创建
                r = client.post("/api/auth/register/verify", json={
                    "username": username, "email": "", "password": password, "code": ""
                })
                assert r.status_code == 200
                data = r.json()
                assert "token" in data
                assert data["user"]["username"] == username

                # 验证账号已创建
                assert is_username_taken(username)

                # 清理
                if data.get("user", {}).get("id"):
                    _cleanup_user(data["user"]["id"])
            finally:
                set_meta_value("allow_signup", "0")

    def test_register_verify_creates_account_with_empty_email(self):
        """无邮箱配置时 email 为空也能成功创建账号。"""
        with patch("src.auth.email_service.is_email_configured", return_value=False):
            from src.user.db import set_meta_value
            set_meta_value("allow_signup", "1")
            try:
                username = "test_empty_email_" + uuid.uuid4().hex[:6]
                r = client.post("/api/auth/register/verify", json={
                    "username": username, "email": "", "password": "TestPass123!", "code": ""
                })
                assert r.status_code == 200
                data = r.json()
                assert data["user"]["username"] == username
                assert data["user"]["email"] == ""

                if data.get("user", {}).get("id"):
                    _cleanup_user(data["user"]["id"])
            finally:
                set_meta_value("allow_signup", "0")

    def test_register_verify_still_checks_code_when_email_configured(self):
        """邮箱已配置时 verify 仍然校验验证码。"""
        with patch("src.auth.email_service.is_email_configured", return_value=True):
            from src.user.db import set_meta_value
            set_meta_value("allow_signup", "1")
            try:
                username = "test_with_email_" + uuid.uuid4().hex[:6]
                email = f"{username}@example.com"
                # 用错误验证码——应该返回 400
                r = client.post("/api/auth/register/verify", json={
                    "username": username, "email": email, "password": "TestPass123!", "code": "000000"
                })
                assert r.status_code == 400
                assert "验证码" in r.json()["detail"]["message"] or "code" in r.json()["detail"]["message"].lower()
            finally:
                set_meta_value("allow_signup", "0")


# ===========================================================================
# 4. 无邮箱配置时忘记密码流程降级
# ===========================================================================

class TestForgotPasswordNoEmailFallback:
    """测试无邮箱配置时忘记密码流程降级。"""

    def test_forgot_password_send_code_returns_503_when_no_email(self):
        """无邮箱配置时 forgot-password/send-code 返回 503。"""
        with patch("src.auth.email_service.is_email_configured", return_value=False):
            r = client.post("/api/auth/forgot-password/send-code", json={
                "email": "test_forgot@example.com"
            })
            assert r.status_code == 503
            data = r.json()
            assert data["detail"]["code"] == 503
            assert data["detail"].get("error_code") == "EMAIL_NOT_CONFIGURED"
            assert "not configured" in data["detail"]["message"].lower()

    def test_forgot_password_send_code_works_when_email_configured(self):
        """邮箱已配置时 forgot-password/send-code 正常返回 200。"""
        with patch("src.auth.email_service.is_email_configured", return_value=True):
            r = client.post("/api/auth/forgot-password/send-code", json={
                "email": "nonexistent_test@example.com"
            })
            assert r.status_code == 200
            assert "验证码已发送" in r.json()["message"]
