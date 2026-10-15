"""D48 缺失修复验证测试。

设计原则：
- 完全 mock DB 函数，不连任何真实数据库（不用 SQLite/MySQL/tempfile）
- 完全 mock 邮件服务，不产生真实邮件发送
- 用 FastAPI dependency_overrides 注入 mock 用户，不走真实鉴权
- 用 unittest.mock.patch 替换所有 DB 函数调用（patch 原始模块路径）
- 不调用 _create_tables()，不导入 session_manager.db

覆盖的修复：
1. 解绑前检查仓库使用情况（需求17）— unbind 返回 409 + 仓库列表
2. 管理员不可禁用自己（需求21）— disable 返回 400
3. 恢复冷静期账号后通知用户（需求22）— restore 后调 send_notification
4. 锁定提示文案（需求2）— check_throttle 返回含"连续登录失败 5 次"
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


_MOCK_USER = {"id": 1, "username": "testuser", "email": "test@example.com", "token_type": "jwt"}
_MOCK_ADMIN = {"id": 10, "username": "admin", "token_type": "admin"}


@pytest.fixture
def client():
    """TestClient with mocked auth dependencies — no real DB, no real JWT."""
    from src.api.main import app
    from src.api.deps import require_jwt_only, require_admin

    app.dependency_overrides[require_jwt_only] = lambda: _MOCK_USER
    app.dependency_overrides[require_admin] = lambda: _MOCK_ADMIN

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def _mock_sm_db(rows):
    """Build mock session_manager.db + db_lock for warehouse queries."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = rows
    mock_db = MagicMock()
    mock_db.cursor.return_value = mock_cursor
    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(return_value=None)
    mock_lock.__exit__ = MagicMock(return_value=False)
    return mock_db, mock_lock


# ═══════════════════════════════════════════════════════════
# 1. 解绑前检查仓库使用情况（需求17）
# ═══════════════════════════════════════════════════════════

class TestUnbindWarehouseCheck:

    @patch("src.api.routes.grape_auth.auth_audit.log_audit")
    @patch("src.api.routes.grape_auth.get_user_identities", return_value=[])
    @patch("src.api.routes.grape_auth.delete_external_identity", return_value=True)
    @patch("src.api.routes.grape_auth.has_password", return_value=True)
    @patch("src.api.routes.grape_auth.count_user_identities", return_value=2)
    @patch("src.user.db.delete_credential")
    def test_unbind_no_active_warehouses(self, mock_del_cred, mock_count, mock_has_pwd,
                                          mock_delete, mock_identities, mock_audit, client):
        """无正在使用仓库时直接解绑成功。"""
        mock_db, mock_lock = _mock_sm_db([])
        with patch("src.session_manager.db", mock_db), \
             patch("src.session_manager.db_lock", mock_lock):
            r = client.delete("/api/auth/bind/gitcode")
        assert r.status_code == 200
        assert "解绑成功" in r.json()["message"]

    @patch("src.api.routes.grape_auth.has_password", return_value=True)
    @patch("src.api.routes.grape_auth.count_user_identities", return_value=2)
    @patch("src.user.db.delete_credential")
    def test_unbind_with_active_warehouses_returns_409(self, mock_del_cred, mock_count, mock_has_pwd, client):
        """有正在使用仓库时返回 409 + 仓库列表。"""
        mock_db, mock_lock = _mock_sm_db([
            ("gitcode:owner/repo1",),
            ("gitcode:owner/repo2",),
        ])
        with patch("src.session_manager.db", mock_db), \
             patch("src.session_manager.db_lock", mock_lock):
            r = client.delete("/api/auth/bind/gitcode")
        assert r.status_code == 409
        d = r.json()["detail"]
        assert d["need_confirm"] is True
        assert len(d["warehouses"]) == 2

    @patch("src.api.routes.grape_auth.auth_audit.log_audit")
    @patch("src.api.routes.grape_auth.get_user_identities", return_value=[])
    @patch("src.api.routes.grape_auth.delete_external_identity", return_value=True)
    @patch("src.api.routes.grape_auth.has_password", return_value=True)
    @patch("src.api.routes.grape_auth.count_user_identities", return_value=2)
    @patch("src.user.db.delete_credential")
    def test_unbind_confirm_bypasses_warehouse_check(self, mock_del_cred, mock_count, mock_has_pwd,
                                                       mock_delete, mock_identities, mock_audit, client):
        """带 confirm=true 跳过仓库检查直接解绑。"""
        mock_db, mock_lock = _mock_sm_db([("gitcode:owner/repo1",)])
        with patch("src.session_manager.db", mock_db), \
             patch("src.session_manager.db_lock", mock_lock):
            r = client.delete("/api/auth/bind/gitcode?confirm=true")
        assert r.status_code == 200
        assert mock_delete.called


# ═══════════════════════════════════════════════════════════
# 2. 管理员不可禁用自己（需求21）
# ═══════════════════════════════════════════════════════════

class TestAdminCannotDisableSelf:

    @patch("src.auth.grape_account_db.get_grape_account_by_id")
    @patch("src.admin.db.disable_grape_user")
    @patch("src.api.deps.invalidate_user_cache")
    @patch("src.auth.audit.log_audit")
    def test_disable_self_returns_400(self, mock_audit, mock_cache, mock_disable, mock_get, client):
        """管理员禁用同用户名的 Grape 账号返回 400。"""
        mock_get.return_value = {"id": 1, "username": "admin", "email": "admin@test.com"}
        r = client.post("/api/admin/users/1/disable", json={"reason": "test"})
        assert r.status_code == 400
        assert "不可禁用自己的账号" in r.json()["detail"]["message"]
        assert not mock_disable.called

    @patch("src.auth.grape_account_db.get_grape_account_by_id")
    @patch("src.admin.db.disable_grape_user", return_value=True)
    @patch("src.api.deps.invalidate_user_cache")
    @patch("src.auth.audit.log_audit")
    def test_disable_other_user_succeeds(self, mock_audit, mock_cache, mock_disable, mock_get, client):
        """管理员禁用其他用户成功。"""
        mock_get.return_value = {"id": 5, "username": "otheruser", "email": "other@test.com"}
        r = client.post("/api/admin/users/5/disable", json={"reason": "violation"})
        assert r.status_code == 200
        assert mock_disable.called


# ═══════════════════════════════════════════════════════════
# 3. 恢复冷静期账号后通知用户（需求22）
# ═══════════════════════════════════════════════════════════

class TestRestoreSendsNotification:

    @patch("src.auth.grape_account_db.get_grape_account_by_id")
    @patch("src.admin.db.restore_grape_user", return_value=True)
    @patch("src.api.deps.invalidate_user_cache")
    @patch("src.auth.audit.log_audit")
    @patch("src.auth.email_service.get_email_service")
    def test_restore_sends_email_when_user_has_email(self, mock_email_svc, mock_audit,
                                                       mock_cache, mock_restore, mock_get, client):
        """恢复有邮箱的用户后发送通知邮件。"""
        mock_get.return_value = {"id": 5, "username": "user5", "email": "user5@test.com"}
        mock_svc = MagicMock()
        mock_email_svc.return_value = mock_svc
        r = client.post("/api/admin/users/5/restore")
        assert r.status_code == 200
        mock_svc.send_notification.assert_called_once()
        assert mock_svc.send_notification.call_args[0][0] == "user5@test.com"

    @patch("src.auth.grape_account_db.get_grape_account_by_id")
    @patch("src.admin.db.restore_grape_user", return_value=True)
    @patch("src.api.deps.invalidate_user_cache")
    @patch("src.auth.audit.log_audit")
    @patch("src.auth.email_service.get_email_service")
    def test_restore_no_email_no_notification(self, mock_email_svc, mock_audit,
                                                mock_cache, mock_restore, mock_get, client):
        """恢复无邮箱的用户不发送通知。"""
        mock_get.return_value = {"id": 5, "username": "user5", "email": ""}
        mock_svc = MagicMock()
        mock_email_svc.return_value = mock_svc
        r = client.post("/api/admin/users/5/restore")
        assert r.status_code == 200
        mock_svc.send_notification.assert_not_called()

    @patch("src.auth.grape_account_db.get_grape_account_by_id")
    @patch("src.admin.db.restore_grape_user", return_value=True)
    @patch("src.api.deps.invalidate_user_cache")
    @patch("src.auth.audit.log_audit")
    @patch("src.auth.email_service.get_email_service")
    def test_restore_email_failure_does_not_block(self, mock_email_svc, mock_audit,
                                                    mock_cache, mock_restore, mock_get, client):
        """通知邮件发送失败不影响恢复操作。"""
        mock_get.return_value = {"id": 5, "username": "user5", "email": "user5@test.com"}
        mock_svc = MagicMock()
        mock_svc.send_notification.side_effect = Exception("SMTP error")
        mock_email_svc.return_value = mock_svc
        r = client.post("/api/admin/users/5/restore")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════
# 4. 锁定提示文案（需求2）
# ═══════════════════════════════════════════════════════════

class TestLockMessage:

    def test_lock_message_contains_failure_count(self):
        """锁定提示包含"连续登录失败 5 次"和"15 分钟"。"""
        from src.auth.login_throttle import (
            ACCOUNT_LOCK_THRESHOLD, ACCOUNT_LOCK_DURATION, check_throttle,
        )
        with patch("src.auth.login_throttle.get_failure_count",
                    return_value=ACCOUNT_LOCK_THRESHOLD), \
             patch("src.auth.login_throttle._get_lock_until",
                    return_value=9999999999.0), \
             patch("src.auth.login_throttle.get_ip_failure_count", return_value=0):
            result = check_throttle("testuser", "127.0.0.1")
        assert not result["allowed"]
        assert result["locked"] is True
        assert str(ACCOUNT_LOCK_THRESHOLD) in result["message"]
        assert str(ACCOUNT_LOCK_DURATION // 60) in result["message"]
        assert "连续登录失败" in result["message"]
        assert "邮箱验证码" in result["message"]
