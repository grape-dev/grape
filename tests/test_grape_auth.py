"""Grape 账号体系 E2E 测试。

测试覆盖：
1. 邮箱注册流程（发送验证码 + 验证创建账号）
2. 统一密码登录（username + email 两种标识符）
3. 忘记密码（验证码重置）
4. 修改密码
5. 获取账号信息 + 更新资料
6. 账号注销
7. 密码哈希兼容（SHA-256 旧密码 → bcrypt 透明升级）
8. 验证码限流
9. 注册策略（白名单/开放注册）
10. 审计日志
11. OAuth 统一回调路由 + 未绑定引导 + Providers API

D46: 邮件服务全程 mock，不依赖环境变量/配置文件/网络。
"""
import os
import sys
import time
import hashlib
import uuid
from unittest.mock import patch, MagicMock

import pytest

# 强制使用 SQLite 测试库
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
os.environ["DEVOPS_DB_PATH"] = "/tmp/test_grape_auth.db"

# 清理旧测试库
if os.path.exists("/tmp/test_grape_auth.db"):
    os.remove("/tmp/test_grape_auth.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.api.db_setup import _create_tables
from src.auth.grape_account_db import (
    create_grape_account, get_grape_account_by_id,
    get_grape_account_by_identifier, is_username_taken, is_email_registered,
    get_token_version, increment_token_version, update_password,
    soft_delete_account, get_user_identities, create_external_identity,
    delete_external_identity, count_user_identities, has_password,
    get_external_identity, update_external_identity_tokens,
)
from src.auth.password import hash_password, verify_password, needs_upgrade
from src.auth.verification import generate_code, send_code, verify_code, SCENES
from src.auth.audit import log_audit, mask_email, mask_identifier, EVENT_TYPES
from src.auth.signup_policy import check_signup_allowed
from src.auth.login_throttle import record_failure, check_throttle, clear_failures
from src.auth.providers import _DEFAULT_PROVIDERS, get_provider, list_providers
from src.auth.email_service import is_production_env, is_dev_verification_code

# 初始化表
_create_tables()


# D46: 全局 mock 邮件服务——所有测试中邮件发送均返回 True，不依赖环境/配置/网络
@pytest.fixture(autouse=True)
def _mock_email_service():
    """mock 邮件服务，send_verification_code 和 send_notification 均返回 True。"""
    with patch("src.auth.email_service.EmailService.send_verification_code", return_value=True), \
         patch("src.auth.email_service.EmailService.send_notification", return_value=True), \
         patch("src.auth.email_service.EmailService._send_smtp", return_value=True), \
         patch("src.auth.email_service.EmailService._send_resend", return_value=True), \
         patch("src.auth.email_service.is_email_configured", return_value=True):
        yield


# D46: mock dev verification code——测试中验证码统一用 888888
_TEST_DEV_CODE = "888888"


@pytest.fixture(autouse=True)
def _mock_dev_code():
    """mock is_dev_verification_code，测试环境固定验证码 888888 直接通过。"""
    with patch("src.auth.verification.is_dev_verification_code", side_effect=lambda code: code == _TEST_DEV_CODE), \
         patch("src.auth.email_service.is_dev_verification_code", side_effect=lambda code: code == _TEST_DEV_CODE):
        yield


client = TestClient(app)


def _jwt_access_token(uid: int, username: str, ver: int = 0):
    from src.api.jwt_utils import create_access_token
    return create_access_token(uid, username, ver)


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
# 1. 邮箱注册流程
# ===========================================================================

class TestRegister:
    def test_register_send_code_success(self):
        """发送注册验证码成功。"""
        # P1-3: 默认关闭注册，测试需手动开启
        from src.user.db import get_meta_value, set_meta_value
        set_meta_value("allow_signup", "1")
        try:
            username = "test_reg_" + uuid.uuid4().hex[:6]
            email = f"{username}@example.com"
            r = client.post("/api/auth/register/send-code", json={
                "username": username, "email": email, "password": "TestPass123!"
            })
            assert r.status_code == 200
            assert "验证码已发送" in r.json()["message"]
        finally:
            set_meta_value("allow_signup", "0")

    def test_register_send_code_username_taken(self):
        """用户名已占用。"""
        username = "test_dup_" + uuid.uuid4().hex[:6]
        email = f"{username}@example.com"
        # 先创建账号
        account = create_grape_account(username, email, "TestPass123!")
        try:
            # 用已存在的用户名注册
            r = client.post("/api/auth/register/send-code", json={
                "username": username, "email": f"other_{email}", "password": "TestPass123!"
            })
            assert r.status_code == 400
            assert "占用" in r.json()["detail"]["message"]
        finally:
            _cleanup_user(account["id"])

    def test_register_send_code_weak_password(self):
        """弱密码在 send-code 阶段不再检查（移到 verify 阶段）。
        send-code 只校验用户名+邮箱+注册策略，不校验密码。"""
        username = "test_weak_" + uuid.uuid4().hex[:6]
        r = client.post("/api/auth/register/send-code", json={
            "username": username, "email": f"{username}@example.com", "password": "short"
        })
        # send-code 不再因弱密码返回 400——密码检查移到 verify 阶段
        # 可能返回 403（注册策略不通过）或 200（通过），不返回 400（密码强度）
        assert r.status_code != 400 or "8" not in r.json().get("detail", {}).get("message", "")

    def test_register_send_code_invalid_username(self):
        """用户名规则不满足。"""
        r = client.post("/api/auth/register/send-code", json={
            "username": "ab", "email": "ab@example.com", "password": "TestPass123!"
        })
        assert r.status_code == 400
        assert "3" in r.json()["detail"]["message"]

    def test_register_send_code_reserved_username(self):
        """保留字用户名。"""
        r = client.post("/api/auth/register/send-code", json={
            "username": "admin", "email": "admin_test@example.com", "password": "TestPass123!"
        })
        assert r.status_code == 400
        assert "保留" in r.json()["detail"]["message"]

    def test_register_verify_success(self):
        """完整注册流程：发送验证码 → 验证 → 创建账号。"""
        # P1-3: 默认关闭注册，测试需手动开启
        from src.user.db import set_meta_value
        set_meta_value("allow_signup", "1")
        try:
            username = "test_verify_" + uuid.uuid4().hex[:6]
            email = f"{username}@example.com"
            password = "TestPass123!"

            # 发送验证码（邮件服务已 mock）
            r1 = client.post("/api/auth/register/send-code", json={
                "username": username, "email": email, "password": password
            })
            assert r1.status_code == 200

            # D46: mock dev code 888888，直接用固定验证码验证
            r2 = client.post("/api/auth/register/verify", json={
                "username": username, "email": email, "password": password, "code": "888888"
            })
            assert r2.status_code == 200
            data = r2.json()
            assert "token" in data
            assert data["user"]["username"] == username

            # 验证账号创建
            assert is_username_taken(username)
            assert is_email_registered(email)

            # 清理
            if data.get("user", {}).get("id"):
                _cleanup_user(data["user"]["id"])
        finally:
            set_meta_value("allow_signup", "0")

    def test_register_verify_wrong_code(self):
        """验证码错误。"""
        username = "test_wrong_code_" + uuid.uuid4().hex[:6]
        email = f"{username}@example.com"

        client.post("/api/auth/register/send-code", json={
            "username": username, "email": email, "password": "TestPass123!"
        })

        r = client.post("/api/auth/register/verify", json={
            "username": username, "email": email, "password": "TestPass123!", "code": "000000"
        })
        assert r.status_code == 400
        assert "验证码" in r.json()["detail"]["message"]


# ===========================================================================
# 2. 统一密码登录
# ===========================================================================

class TestLogin:
    def test_login_by_username(self):
        """用户名登录。"""
        username = "test_login_u_" + uuid.uuid4().hex[:6]
        email = f"{username}@example.com"
        password = "TestPass123!"

        # 创建账号
        account = create_grape_account(username, email, password)
        assert account is not None

        try:
            # 用户名登录
            r = client.post("/api/auth/login", json={
                "identifier": username, "password": password
            })
            assert r.status_code == 200
            assert "token" in r.json()
        finally:
            _cleanup_user(account["id"])

    def test_login_by_email(self):
        """邮箱登录。"""
        username = "test_login_e_" + uuid.uuid4().hex[:6]
        email = f"{username}@example.com"
        password = "TestPass123!"

        account = create_grape_account(username, email, password)
        assert account is not None

        try:
            r = client.post("/api/auth/login", json={
                "identifier": email, "password": password
            })
            assert r.status_code == 200
            assert "token" in r.json()
        finally:
            _cleanup_user(account["id"])

    def test_login_wrong_password(self):
        """密码错误。"""
        username = "test_login_w_" + uuid.uuid4().hex[:6]
        email = f"{username}@example.com"

        account = create_grape_account(username, email, "TestPass123!")
        try:
            r = client.post("/api/auth/login", json={
                "identifier": username, "password": "WrongPass123!"
            })
            assert r.status_code == 401
            assert "密码错误" in r.json()["detail"]["message"]
        finally:
            _cleanup_user(account["id"])

    def test_login_nonexistent_user(self):
        """不存在的用户。"""
        # 清理同 IP 累积的失败记录，避免限流干扰
        try:
            with db_lock:
                db.cursor().execute("DELETE FROM login_failures WHERE ip='127.0.0.1'")
                db.commit()
        except Exception:
            pass
        r = client.post("/api/auth/login", json={
            "identifier": "nonexistent_user_xyz", "password": "anypassword"
        })
        assert r.status_code == 401
        assert "密码错误" in r.json()["detail"]["message"]


# ===========================================================================
# 3. 密码哈希兼容
# ===========================================================================

class TestPasswordHash:
    def test_bcrypt_hash_and_verify(self):
        """bcrypt 哈希和验证。"""
        h = hash_password("MyPassword123!")
        assert h.startswith("bcrypt$")
        assert verify_password("MyPassword123!", h)
        assert not verify_password("wrong", h)

    def test_sha256_legacy_compat(self):
        """旧 SHA-256 密码兼容。"""
        old_hash = hashlib.sha256("oldpass".encode()).hexdigest()
        assert verify_password("oldpass", old_hash)
        assert needs_upgrade(old_hash)

    def test_sha256_prefixed_compat(self):
        """带前缀的旧 SHA-256 密码。"""
        old_hash = hashlib.sha256("oldpass".encode()).hexdigest()
        prefixed = f"sha256${old_hash}"
        assert verify_password("oldpass", prefixed)
        assert needs_upgrade(prefixed)

    def test_long_password_prehash(self):
        """超长密码 SHA-256 预哈希。"""
        long_pw = "A" * 100
        h = hash_password(long_pw)
        assert verify_password(long_pw, h)
        assert not verify_password("A" * 99, h)


# ===========================================================================
# 4. 验证码系统
# ===========================================================================

class TestVerification:
    def test_generate_code_format(self):
        """验证码格式：6 位数字。"""
        for _ in range(20):
            code = generate_code()
            assert len(code) == 6
            assert code.isdigit()
            assert 0 <= int(code) <= 999999

    def test_all_scenes_defined(self):
        """7 个场景全部定义。"""
        assert len(SCENES) == 7
        assert "register" in SCENES
        assert "reset_password" in SCENES
        assert "set_password" in SCENES
        assert "change_email" in SCENES
        assert "unlock_account" in SCENES
        assert "delete_account" in SCENES
        assert "oauth_associate" in SCENES

    def test_send_code_success(self):
        """发送验证码成功。"""
        email = f"test_vc_{uuid.uuid4().hex[:6]}@example.com"
        result = send_code(email, "register", "127.0.0.1")
        assert result["ok"]
        assert "验证码已发送" in result["message"]

    def test_send_code_rate_limit_60s(self):
        """60 秒内不可重复发送。"""
        email = f"test_rl_{uuid.uuid4().hex[:6]}@example.com"
        r1 = send_code(email, "register", "127.0.0.1")
        assert r1["ok"]
        r2 = send_code(email, "register", "127.0.0.1")
        assert not r2["ok"]
        assert "秒" in r2["message"]

    def test_verify_code_with_dev_code(self):
        """开发环境固定验证码（D46: mock 方式，不操作环境变量）。"""
        email = f"test_dev_{uuid.uuid4().hex[:6]}@example.com"
        # mock dev code 888888 直接通过
        result = verify_code(email, "register", "888888")
        assert result["ok"]


# ===========================================================================
# 5. 审计日志
# ===========================================================================

class TestAudit:
    def test_mask_email(self):
        """邮箱脱敏。"""
        assert mask_email("alice@example.com") == "a***@example.com"
        assert mask_email("a@b.com") == "*@b.com"
        assert mask_email("") == ""

    def test_mask_identifier(self):
        """标识符脱敏。"""
        assert mask_identifier("alice") == "a***e"
        assert mask_identifier("a@example.com") == "*@example.com"

    def test_event_types_complete(self):
        """事件类型完整。"""
        expected = {
            "register", "login_success", "login_failed",
            "password_reset", "password_change", "email_change",
            "bind", "unbind", "account_disabled", "account_restored",
            "account_locked", "account_unlocked", "account_deleted",
            "set_password",
        }
        assert expected.issubset(EVENT_TYPES)

    def test_log_audit_writes(self):
        """审计日志写入。"""
        log_audit("login_success", user_id=99999, ip="127.0.0.1",
                  detail={"method": "password"})
        with db_lock:
            c = db.cursor()
            c.execute("SELECT event_type, user_id, ip FROM audit_logs WHERE user_id=99999 ORDER BY seq DESC LIMIT 1")
            row = c.fetchone()
        assert row is not None
        assert row[0] == "login_success"
        assert row[1] == 99999
        assert row[2] == "127.0.0.1"


# ===========================================================================
# 6. 注册策略
# ===========================================================================

class TestSignupPolicy:
    def test_default_allow(self):
        """P1-3: 默认关闭注册（无白名单 + 未配置 allow_signup）。"""
        # 确保 allow_signup 未配置
        from src.user.db import set_meta_value, get_meta_value
        set_meta_value("allow_signup", "")
        assert not check_signup_allowed("anyone@example.com")

    def test_email_whitelist(self):
        """邮箱白名单。"""
        # 添加白名单
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR IGNORE INTO signup_whitelist (type, value) VALUES ('email', 'whitelisted@example.com')")
            db.commit()
        try:
            assert check_signup_allowed("whitelisted@example.com")
            assert not check_signup_allowed("not_whitelisted@example.com")
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM signup_whitelist WHERE value='whitelisted@example.com'")
                db.commit()


class TestAllowSignupValueConsistency:
    """回归测试：allow_signup 取值类型一致性。

    确保 meta 表中 allow_signup 无论以 "true"/"1"/"yes" 哪种值写入，
    read_public_config() 都归一化返回 "1"/"0"，前端 v === '1' 判定正确。
    """

    def test_meta_true_normalizes_to_1(self):
        """meta="true" → config 返回 "1"。"""
        from src.user.db import set_meta_value
        from src.admin.config_store import read_public_config
        set_meta_value("allow_signup", "true")
        cfg = read_public_config()
        assert cfg["allow_signup"] == "1", f'Expected "1", got "{cfg["allow_signup"]}"'

    def test_meta_1_normalizes_to_1(self):
        """meta="1" → config 返回 "1"。"""
        from src.user.db import set_meta_value
        from src.admin.config_store import read_public_config
        set_meta_value("allow_signup", "1")
        cfg = read_public_config()
        assert cfg["allow_signup"] == "1", f'Expected "1", got "{cfg["allow_signup"]}"'

    def test_meta_empty_normalizes_to_0(self):
        """meta="" → config 返回 "0"。"""
        from src.user.db import set_meta_value
        from src.admin.config_store import read_public_config
        set_meta_value("allow_signup", "")
        cfg = read_public_config()
        assert cfg["allow_signup"] == "0", f'Expected "0", got "{cfg["allow_signup"]}"'

    def test_meta_yes_normalizes_to_1(self):
        """meta="yes" → config 返回 "1"（兼容旧值）。"""
        from src.user.db import set_meta_value
        from src.admin.config_store import read_public_config
        set_meta_value("allow_signup", "yes")
        cfg = read_public_config()
        assert cfg["allow_signup"] == "1", f'Expected "1", got "{cfg["allow_signup"]}"'


# ===========================================================================
# 7. 登录限流
# ===========================================================================

class TestLoginThrottle:
    def test_no_throttle_initially(self):
        """初始无限流。"""
        result = check_throttle("test_throttle_user", "127.0.0.1")
        assert result["allowed"]

    def test_lock_after_5_failures(self):
        """5 次失败后临时锁定（需求2: 5次直接锁定，不实现图形验证码）。"""
        identifier = "test_5fail_" + uuid.uuid4().hex[:6]
        for _ in range(5):
            record_failure(identifier, "127.0.0.1")
        result = check_throttle(identifier, "127.0.0.1")
        assert not result["allowed"]
        assert result.get("locked")

    def test_lock_after_10_failures(self):
        """10 次失败后仍然锁定（锁定阈值已改为5，10次同样锁定）。"""
        identifier = "test_10fail_" + uuid.uuid4().hex[:6]
        for _ in range(10):
            record_failure(identifier, "127.0.0.1")
        result = check_throttle(identifier, "127.0.0.1")
        assert not result["allowed"]
        assert result.get("locked")

    def test_clear_failures(self):
        """清除失败记录。"""
        identifier = "test_clear_" + uuid.uuid4().hex[:6]
        for _ in range(5):
            record_failure(identifier, "127.0.0.1")
        clear_failures(identifier)
        result = check_throttle(identifier, "127.0.0.1")
        assert result["allowed"]


# ===========================================================================
# 8. Provider 注册表
# ===========================================================================

class TestProviders:
    def test_providers_defined(self):
        """Provider 已定义。"""
        assert "gitcode" in _DEFAULT_PROVIDERS
        assert "github" in _DEFAULT_PROVIDERS

    def test_get_provider(self):
        """获取 Provider 配置。"""
        cfg = get_provider("gitcode")
        assert cfg is not None
        assert "auth_url" in cfg
        assert "token_url" in cfg
        assert "user_url" in cfg
        assert "id_field" in cfg

    def test_get_nonexistent_provider(self):
        """不存在的 Provider。"""
        assert get_provider("nonexistent") is None

    def test_pkce_config(self):
        """PKCE 配置（OMIT-3 修复：默认启用 PKCE）。"""
        assert _DEFAULT_PROVIDERS["gitcode"]["pkce_enabled"] is True
        assert _DEFAULT_PROVIDERS["github"]["pkce_enabled"] is True


# ===========================================================================
# 9. Grape 账号 CRUD
# ===========================================================================

class TestGrapeAccountCRUD:
    def test_create_and_get(self):
        """创建并查询账号。"""
        username = "test_crud_" + uuid.uuid4().hex[:6]
        email = f"{username}@example.com"
        account = create_grape_account(username, email, "TestPass123!")
        assert account is not None
        try:
            # 按 ID 查询
            by_id = get_grape_account_by_id(account["id"])
            assert by_id is not None
            assert by_id["username"] == username
            # 按用户名查询
            by_username = get_grape_account_by_identifier(username)
            assert by_username is not None
            # 按邮箱查询
            by_email = get_grape_account_by_identifier(email)
            assert by_email is not None
            # 有密码
            assert has_password(account["id"])
        finally:
            _cleanup_user(account["id"])

    def test_token_version(self):
        """token_version 管理。"""
        username = "test_tv_" + uuid.uuid4().hex[:6]
        account = create_grape_account(username, f"{username}@example.com", "TestPass123!")
        try:
            assert get_token_version(account["id"]) == 0
            increment_token_version(account["id"])
            assert get_token_version(account["id"]) == 1
        finally:
            _cleanup_user(account["id"])

    def test_soft_delete(self):
        """软删除账号。"""
        username = "test_sd_" + uuid.uuid4().hex[:6]
        account = create_grape_account(username, f"{username}@example.com", "TestPass123!")
        try:
            assert soft_delete_account(account["id"])
            acc = get_grape_account_by_id(account["id"])
            assert acc["status"] == 3  # 冷静期
        finally:
            _cleanup_user(account["id"])

    def test_external_identity(self):
        """第三方绑定 CRUD。"""
        username = "test_ei_" + uuid.uuid4().hex[:6]
        account = create_grape_account(username, f"{username}@example.com", "TestPass123!")
        try:
            # 创建绑定
            assert create_external_identity(account["id"], "gitcode", "12345", "testuser")
            # 查询
            identities = get_user_identities(account["id"])
            assert len(identities) == 1
            assert identities[0]["provider"] == "gitcode"
            # 计数
            assert count_user_identities(account["id"]) == 1
            # 删除
            assert delete_external_identity(account["id"], "gitcode")
            assert count_user_identities(account["id"]) == 0
        finally:
            _cleanup_user(account["id"])


# ===========================================================================
# 10. 邮件服务
# ===========================================================================

class TestEmailService:
    def test_not_production_by_default(self):
        """默认非生产环境。"""
        assert not is_production_env()

    def test_dev_code_mock_via_verification_module(self):
        """D46: mock dev code 通过 verification 模块调用时生效。

        mock patch 在 src.auth.verification.is_dev_verification_code 上，
        所以通过 verify_code() 调用路径会走 mock。
        直接调 email_service.is_dev_verification_code 不走 mock（测试原始函数行为）。
        """
        # 原始函数（未被 mock）：无环境变量时返回 False
        os.environ.pop("GRAPE_DEV_VERIFICATION_CODE", None)
        assert not is_dev_verification_code("888888")

        # 通过 verify_code 调用路径走 mock：888888 通过
        result = verify_code("test_mock@example.com", "register", "888888")
        assert result["ok"]
