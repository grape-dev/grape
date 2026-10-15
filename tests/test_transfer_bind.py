"""binding_ticket 转移绑定单元测试。

全部 DB 操作通过 mock 完成，不操作真实数据库文件。
测试覆盖：
1. save_binding_ticket / validate_binding_ticket 票据生命周期
2. transfer_external_identity 原子转移（mock DB cursor）
3. POST /api/auth/transfer-bind API（dependency_overrides + mock）
"""
import os
import time
from unittest.mock import patch, MagicMock, ANY

import pytest

# 强制使用 SQLite 测试库（测试环境隔离）
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
os.environ["DEVOPS_DB_PATH"] = "/tmp/test_transfer_bind.db"
if os.path.exists("/tmp/test_transfer_bind.db"):
    os.remove("/tmp/test_transfer_bind.db")

from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """创建测试客户端，注入 mock JWT 依赖。"""
    from src.api.main import app
    from src.api.routes.grape_auth import require_jwt_only
    from src.session_manager import db, db_lock

    # 清理 user_id=200 的残留数据（防止跨文件测试污染）
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM external_identities WHERE grape_user_id=200")
        c.execute("DELETE FROM grape_accounts WHERE id=200")
        db.commit()

    mock_user = {"id": 200, "username": "user_b", "role": "user"}
    app.dependency_overrides[require_jwt_only] = lambda: mock_user
    yield TestClient(app)
    app.dependency_overrides.clear()


# ===========================================================================
# 1. 票据生命周期测试（真实 SQLite 测试库，票据存 DB）
# ===========================================================================

class TestBindingTicketLifecycle:
    """票据生成、校验、消费、过期。"""

    def test_save_and_validate_ticket(self):
        """生成票据后能成功校验并消费。"""
        from src.auth.grape_account_db import save_binding_ticket, validate_binding_ticket

        ticket = save_binding_ticket(
            initiating_user_id=100,
            provider="gitcode",
            external_id="gc_abc123",
            external_login="testuser",
            original_user_id=99,
        )
        assert ticket is not None
        assert len(ticket) > 20

        data = validate_binding_ticket(ticket, 100)
        assert data is not None
        assert data["provider"] == "gitcode"
        assert data["external_id"] == "gc_abc123"
        assert data["original_user_id"] == 99

    def test_ticket_one_time_consumption(self):
        """票据一次性消费——第二次校验返回 None。"""
        from src.auth.grape_account_db import save_binding_ticket, validate_binding_ticket

        ticket = save_binding_ticket(
            initiating_user_id=101,
            provider="github",
            external_id="gh_456",
            original_user_id=50,
        )
        assert ticket is not None
        data = validate_binding_ticket(ticket, 101)
        assert data is not None
        data2 = validate_binding_ticket(ticket, 101)
        assert data2 is None

    def test_ticket_user_mismatch(self):
        """票据 initiating_user_id 不匹配时返回 None。"""
        from src.auth.grape_account_db import save_binding_ticket, validate_binding_ticket

        ticket = save_binding_ticket(
            initiating_user_id=200,
            provider="gitcode",
            external_id="gc_789",
            original_user_id=100,
        )
        assert ticket is not None
        data = validate_binding_ticket(ticket, 999)
        assert data is None

    def test_ticket_invalid(self):
        """不存在的票据返回 None。"""
        from src.auth.grape_account_db import validate_binding_ticket
        data = validate_binding_ticket("nonexistent_ticket", 100)
        assert data is None


# ===========================================================================
# 2. transfer_external_identity 测试（mock DB cursor）
# ===========================================================================

class TestTransferExternalIdentity:
    """原子转移绑定——mock DB 层验证逻辑。"""

    @patch("src.auth.credential_crypto.encrypt")
    @patch("src.auth.grape_account_db.db")
    @patch("src.auth.grape_account_db.db_lock")
    def test_transfer_success(self, mock_lock, mock_db, mock_encrypt):
        """转移成功——旧绑定 status=0，新绑定 INSERT。"""
        from src.auth.grape_account_db import transfer_external_identity

        mock_cursor = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        mock_encrypt.return_value = "encrypted_token"

        result = transfer_external_identity(
            new_user_id=200,
            provider="gitcode",
            external_id="gc_123",
            external_login="testuser",
            access_token="token123",
            original_user_id=100,
        )

        assert result is True
        assert mock_cursor.execute.call_count >= 3
        mock_db.commit.assert_called_once()

    @patch("src.auth.credential_crypto.encrypt")
    @patch("src.auth.grape_account_db.db")
    @patch("src.auth.grape_account_db.db_lock")
    def test_transfer_no_original_user(self, mock_lock, mock_db, mock_encrypt):
        """original_user_id=0 时跳过旧绑定 UPDATE，只 SELECT status=1 + DELETE + INSERT。"""
        from src.auth.grape_account_db import transfer_external_identity

        mock_cursor = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        mock_encrypt.return_value = "enc"
        # fetchone 返回 None：目标用户无活跃绑定，跳过 status=0 UPDATE
        mock_cursor.fetchone.return_value = None

        result = transfer_external_identity(
            new_user_id=200,
            provider="github",
            external_id="gh_456",
            original_user_id=0,
        )

        assert result is True
        # original_user_id=0 跳过旧绑定 UPDATE，目标用户无活跃绑定：
        # SELECT status=1(1) + DELETE provider+external_id(1) + DELETE grape_user_id+provider(1) + INSERT(1) = 4
        assert mock_cursor.execute.call_count == 4

    @patch("src.auth.credential_crypto.encrypt")
    @patch("src.auth.grape_account_db.db")
    @patch("src.auth.grape_account_db.db_lock")
    def test_transfer_db_error_returns_false(self, mock_lock, mock_db, mock_encrypt):
        """DB 异常时返回 False 并 rollback。"""
        from src.auth.grape_account_db import transfer_external_identity

        mock_cursor = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        mock_cursor.execute.side_effect = Exception("DB error")

        result = transfer_external_identity(
            new_user_id=200,
            provider="gitcode",
            external_id="gc_123",
            original_user_id=100,
        )

        assert result is False
        mock_db.rollback.assert_called_once()


# ===========================================================================
# 2b. is_placeholder_account_mergeable / _has_business_data 测试
# ===========================================================================

class TestPlaceholderAccountMerge:
    """占位账号可合并性检查——mock DB 层验证逻辑。"""

    @patch("src.auth.grape_account_db.db")
    @patch("src.auth.grape_account_db.db_lock")
    def test_mergeable_no_business_data(self, mock_lock, mock_db):
        """纯占位账号（无密码、无其他绑定、无业务数据）→ 可自动转移。"""
        from src.auth.grape_account_db import is_placeholder_account_mergeable

        mock_cursor = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        # grape_accounts 查询返回：email_is_placeholder=1, created_by_oauth=1, password_hash='', status=1
        mock_cursor.fetchone.side_effect = [
            (1, 1, "", 1),  # grape_accounts row
            (1,),           # external_identities count = 1
        ]
        # _has_business_data: 所有表返回 count=0
        # fetchone 在 _has_business_data 循环中每张表调一次，全部返回 (0,)
        # 但 side_effect 已用完上面的两次，需要额外补充
        mock_cursor.fetchone.side_effect = [
            (1, 1, "", 1),  # grape_accounts
            (1,),           # external_identities count
        ] + [(0,)] * 20  # _has_business_data 的每张表返回 0

        result = is_placeholder_account_mergeable(100)
        assert result is True

    @patch("src.auth.grape_account_db.db")
    @patch("src.auth.grape_account_db.db_lock")
    def test_not_mergeable_has_business_data(self, mock_lock, mock_db):
        """占位账号有业务数据 → 不可自动转移。"""
        from src.auth.grape_account_db import is_placeholder_account_mergeable

        mock_cursor = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        # 第一张业务表（user_repos）返回 count=1 → 有业务数据
        mock_cursor.fetchone.side_effect = [
            (1, 1, "", 1),  # grape_accounts
            (1,),           # external_identities count = 1
            (1,),           # user_repos is_admin=1 count = 1 → 有业务数据
        ]

        result = is_placeholder_account_mergeable(100)
        assert result is False

    @patch("src.auth.grape_account_db.db")
    @patch("src.auth.grape_account_db.db_lock")
    def test_not_mergeable_has_password(self, mock_lock, mock_db):
        """有密码的账号 → 不可自动转移。"""
        from src.auth.grape_account_db import is_placeholder_account_mergeable

        mock_cursor = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1, 1, "hashed_password", 1)

        result = is_placeholder_account_mergeable(100)
        assert result is False

    @patch("src.auth.grape_account_db.db")
    @patch("src.auth.grape_account_db.db_lock")
    def test_not_mergeable_not_placeholder(self, mock_lock, mock_db):
        """非占位账号 → 不可自动转移。"""
        from src.auth.grape_account_db import is_placeholder_account_mergeable

        mock_cursor = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (0, 0, "", 1)

        result = is_placeholder_account_mergeable(100)
        assert result is False

    @patch("src.auth.grape_account_db.db")
    @patch("src.auth.grape_account_db.db_lock")
    def test_not_mergeable_multiple_bindings(self, mock_lock, mock_db):
        """有多个活跃绑定的账号 → 不可自动转移。"""
        from src.auth.grape_account_db import is_placeholder_account_mergeable

        mock_cursor = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [
            (1, 1, "", 1),  # grape_accounts
            (2,),           # external_identities count = 2
        ]

        result = is_placeholder_account_mergeable(100)
        assert result is False


# ===========================================================================
# 3. POST /api/auth/transfer-bind API 测试（dependency_overrides + mock）
# ===========================================================================

class TestTransferBindAPI:
    """转移绑定 API——通过 dependency_overrides 注入 mock 用户，mock DB 函数。"""

    @patch("src.api.routes.grape_auth.get_grape_account_by_id")
    @patch("src.api.routes.grape_auth.transfer_business_data")
    @patch("src.api.routes.grape_auth.transfer_external_identity_in_tx")
    @patch("src.api.routes.grape_auth.validate_binding_ticket")
    @patch("src.api.routes.grape_auth.has_password")
    @patch("src.api.routes.grape_auth.count_user_identities")
    @patch("src.api.routes.grape_auth.auth_audit")
    @patch("src.api.routes.grape_auth._clear_user_repos_cache")
    def test_transfer_success(
        self, mock_clear, mock_audit, mock_count, mock_has_pwd, mock_validate,
        mock_transfer_in_tx, mock_transfer_biz, mock_get_account, client,
    ):
        """完整的转移成功流程（非占位账号，有密码）。"""
        mock_validate.return_value = {
            "provider": "gitcode",
            "external_id": "gc_123",
            "external_login": "testuser",
            "external_name": "Test",
            "external_avatar": "",
            "original_user_id": 100,
        }
        mock_has_pwd.return_value = True
        mock_count.return_value = 1
        mock_get_account.return_value = {"email_is_placeholder": 0, "created_by_oauth": 0}
        mock_transfer_biz.return_value = True
        mock_transfer_in_tx.return_value = True

        resp = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": "valid_ticket",
            "confirm": True,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert "转移成功" in data["message"]
        mock_transfer_biz.assert_called_once()
        mock_transfer_in_tx.assert_called_once()
        mock_audit.log_audit.assert_called_once()
        assert mock_clear.call_count == 2

    @patch("src.api.routes.grape_auth.validate_binding_ticket")
    def test_transfer_invalid_ticket(self, mock_validate, client):
        """票据无效/过期 → 410。"""
        mock_validate.return_value = None

        resp = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": "invalid_ticket",
            "confirm": True,
        })

        assert resp.status_code == 410

    @patch("src.api.routes.grape_auth.validate_binding_ticket")
    def test_transfer_not_confirmed(self, mock_validate, client):
        """用户未确认 → 400。"""
        mock_validate.return_value = {
            "provider": "gitcode",
            "external_id": "gc_123",
            "original_user_id": 100,
            "external_login": "",
            "external_name": "",
            "external_avatar": "",
        }

        resp = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": "valid_ticket",
            "confirm": False,
        })

        assert resp.status_code == 400

    @patch("src.api.routes.grape_auth.get_grape_account_by_id")
    @patch("src.api.routes.grape_auth.validate_binding_ticket")
    @patch("src.api.routes.grape_auth.has_password")
    @patch("src.api.routes.grape_auth.count_user_identities")
    def test_transfer_original_no_login_method(
        self, mock_count, mock_has_pwd, mock_validate, mock_get_account, client,
    ):
        """原账号无其他登录方式 → 400（非占位账号）。"""
        mock_validate.return_value = {
            "provider": "gitcode",
            "external_id": "gc_123",
            "original_user_id": 100,
            "external_login": "",
            "external_name": "",
            "external_avatar": "",
        }
        mock_get_account.return_value = {"email_is_placeholder": 0, "created_by_oauth": 0}
        mock_has_pwd.return_value = False
        mock_count.return_value = 1

        resp = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": "valid_ticket",
            "confirm": True,
        })

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "无法访问" in detail["message"] or "联系客服" in detail["message"]

    @patch("src.api.routes.grape_auth.get_grape_account_by_id")
    @patch("src.api.routes.grape_auth.transfer_business_data")
    @patch("src.api.routes.grape_auth.transfer_external_identity_in_tx")
    @patch("src.api.routes.grape_auth.validate_binding_ticket")
    @patch("src.api.routes.grape_auth.has_password")
    @patch("src.api.routes.grape_auth.count_user_identities")
    def test_transfer_db_failure(
        self, mock_count, mock_has_pwd, mock_validate,
        mock_transfer_in_tx, mock_transfer_biz, mock_get_account, client,
    ):
        """转移 DB 失败 → 500。"""
        mock_validate.return_value = {
            "provider": "gitcode",
            "external_id": "gc_123",
            "original_user_id": 100,
            "external_login": "",
            "external_name": "",
            "external_avatar": "",
        }
        mock_get_account.return_value = {"email_is_placeholder": 0, "created_by_oauth": 0}
        mock_has_pwd.return_value = True
        mock_count.return_value = 1
        mock_transfer_biz.return_value = True
        mock_transfer_in_tx.return_value = False

        resp = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": "valid_ticket",
            "confirm": True,
        })

        assert resp.status_code == 500

    @patch("src.api.routes.grape_auth.get_grape_account_by_id")
    @patch("src.api.routes.grape_auth.transfer_business_data")
    @patch("src.api.routes.grape_auth.transfer_external_identity_in_tx")
    @patch("src.api.routes.grape_auth.validate_binding_ticket")
    @patch("src.api.routes.grape_auth.has_password")
    @patch("src.api.routes.grape_auth.count_user_identities")
    def test_transfer_original_has_other_oauth(
        self, mock_count, mock_has_pwd, mock_validate,
        mock_transfer_in_tx, mock_transfer_biz, mock_get_account, client,
    ):
        """原账号有其他 OAuth 绑定 → 允许转移。"""
        mock_validate.return_value = {
            "provider": "gitcode",
            "external_id": "gc_123",
            "original_user_id": 100,
            "external_login": "",
            "external_name": "",
            "external_avatar": "",
        }
        mock_get_account.return_value = {"email_is_placeholder": 0, "created_by_oauth": 0}
        mock_has_pwd.return_value = False
        mock_count.return_value = 2  # 2 个活跃绑定，说明还有 1 个其他 OAuth
        mock_transfer_biz.return_value = True
        mock_transfer_in_tx.return_value = True

        resp = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": "valid_ticket",
            "confirm": True,
        })

        assert resp.status_code == 200

    @patch("src.api.routes.grape_auth.get_grape_account_by_id")
    @patch("src.api.routes.grape_auth.transfer_business_data")
    @patch("src.api.routes.grape_auth.transfer_external_identity_in_tx")
    @patch("src.api.routes.grape_auth.validate_binding_ticket")
    @patch("src.api.routes.grape_auth.has_password")
    @patch("src.api.routes.grape_auth.count_user_identities")
    def test_transfer_placeholder_with_business_data(
        self, mock_count, mock_has_pwd, mock_validate,
        mock_transfer_in_tx, mock_transfer_biz, mock_get_account, client,
    ):
        """占位账号有业务数据 → 走转移确认流程，跳过登录方式检查，标记 merged。"""
        mock_validate.return_value = {
            "provider": "gitcode",
            "external_id": "gc_123",
            "original_user_id": 100,
            "external_login": "",
            "external_name": "",
            "external_avatar": "",
        }
        # 占位账号：有业务数据，无密码，只有 1 个绑定
        mock_get_account.return_value = {"email_is_placeholder": 1, "created_by_oauth": 1}
        mock_has_pwd.return_value = False
        mock_count.return_value = 1  # 只有正在转移的这一条
        mock_transfer_biz.return_value = True
        mock_transfer_in_tx.return_value = True

        resp = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": "valid_ticket",
            "confirm": True,
        })

        # 占位账号跳过登录方式检查 → 应该成功
        assert resp.status_code == 200
        assert "转移成功" in resp.json()["message"]
        mock_transfer_biz.assert_called_once()
        mock_transfer_in_tx.assert_called_once()

    @patch("src.api.routes.grape_auth.get_grape_account_by_id")
    @patch("src.api.routes.grape_auth.transfer_business_data")
    @patch("src.api.routes.grape_auth.transfer_external_identity_in_tx")
    @patch("src.api.routes.grape_auth.validate_binding_ticket")
    @patch("src.api.routes.grape_auth.has_password")
    @patch("src.api.routes.grape_auth.count_user_identities")
    def test_transfer_placeholder_biz_data_failure(
        self, mock_count, mock_has_pwd, mock_validate,
        mock_transfer_in_tx, mock_transfer_biz, mock_get_account, client,
    ):
        """占位账号业务数据转移失败 → 500 回滚。"""
        mock_validate.return_value = {
            "provider": "gitcode",
            "external_id": "gc_123",
            "original_user_id": 100,
            "external_login": "",
            "external_name": "",
            "external_avatar": "",
        }
        mock_get_account.return_value = {"email_is_placeholder": 1, "created_by_oauth": 1}
        mock_has_pwd.return_value = False
        mock_count.return_value = 1
        mock_transfer_biz.return_value = False  # 业务数据转移失败
        mock_transfer_in_tx.return_value = True

        resp = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": "valid_ticket",
            "confirm": True,
        })

        assert resp.status_code == 500
