"""Issue #3469 路由层用例（HTTP 契约，不连真实库）。

领域规则已在 `test_issue_3469_email_change_service.py` 覆盖；本文件只锁**路由接线**：

- `change-email/send-code` 回 `require_factor`，领域错误 → 状态码 + error_code + require_factor
- `change-email` 的 400/409/429 映射（P）
- `set-password/send-code` 对不可投递邮箱回 400 `EMAIL_UNBOUND`，不再是 429（D / 验收 4）
- `/auth/me` 暴露 `email_deliverable`（验收 11 的前端判据）
- 生产环境邮件未配置时 `register/verify` fail-closed 503（K）
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.domains.mgmt.auth.email_change import (
    EmailChangeError,
    EmailChangeResult,
    EmailFactor,
)

USER = {"id": "u-3469", "username": "tester", "token_type": "jwt"}


@pytest.fixture(scope="session")
def client():
    from src.api.main import app
    from src.api.deps import require_jwt_only, require_no_session_token

    app.dependency_overrides[require_jwt_only] = lambda: dict(USER)
    # /auth/me 走另一条依赖（user_only），同样用 override 注入，不靠 patch 模块属性
    app.dependency_overrides[require_no_session_token] = lambda: dict(USER)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _error(status, code, message, factor=None):
    return EmailChangeError(status, code, message, require_factor=factor)


# ── change-email/send-code ──────────────────────────────────────────
def test_send_code_returns_require_factor(client):
    with patch("src.domains.mgmt.auth.email_change.EmailChangeService.request_code",
               return_value=EmailChangeResult("验证码已发送", EmailFactor.PASSWORD)), \
         patch("src.domains.mgmt.auth.email_service.is_email_configured", return_value=True):
        r = client.post("/api/auth/change-email/send-code", json={"new_email": "new@example.com"})
    assert r.status_code == 200
    assert r.json()["require_factor"] == "password"


@pytest.mark.parametrize("status,code,factor,expected_status", [
    (400, "EMAIL_FORMAT", None, 400),
    (400, "EMAIL_SAME", None, 400),
    (400, "EMAIL_TAKEN", None, 400),
    (429, "CHANGE_EMAIL_TOO_OFTEN", None, 429),
])
def test_send_code_maps_domain_errors(client, status, code, factor, expected_status):
    with patch("src.domains.mgmt.auth.email_change.EmailChangeService.request_code",
               side_effect=_error(status, code, "x", factor)), \
         patch("src.domains.mgmt.auth.email_service.is_email_configured", return_value=True):
        r = client.post("/api/auth/change-email/send-code", json={"new_email": "new@example.com"})
    assert r.status_code == expected_status
    assert r.json()["detail"]["error_code"] == code


# ── change-email 提交 ──────────────────────────────────────────────
def test_commit_needs_second_factor_maps_require_factor(client):
    with patch("src.domains.mgmt.auth.email_change.EmailChangeService.commit",
               side_effect=_error(400, "NEED_SECOND_FACTOR", "请输入当前密码", EmailFactor.PASSWORD)):
        r = client.post("/api/auth/change-email",
                        json={"new_email": "new@example.com", "code": "123456"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error_code"] == "NEED_SECOND_FACTOR"
    assert detail["require_factor"] == "password"


def test_commit_conflict_returns_409(client):
    with patch("src.domains.mgmt.auth.email_change.EmailChangeService.commit",
               side_effect=_error(409, "EMAIL_TAKEN", "该邮箱已被占用")):
        r = client.post("/api/auth/change-email",
                        json={"new_email": "new@example.com", "code": "123456"})
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "EMAIL_TAKEN"


# ── D：不可投递邮箱按"未绑定"处理 ──────────────────────────────────
@pytest.mark.parametrize("email", ["1+u@noreply.gitcode.com", "未公开邮箱"])
def test_set_password_send_code_rejects_undeliverable_mailbox(client, email):
    account = {"id": USER["id"], "email": email, "password_hash": None}
    with patch("src.domains.mgmt.routes.grape_auth.get_grape_account_by_id", return_value=account), \
         patch("src.domains.mgmt.routes.grape_auth.has_password", return_value=False), \
         patch("src.domains.mgmt.auth.email_service.is_email_configured", return_value=True):
        r = client.post("/api/auth/set-password/send-code")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "EMAIL_UNBOUND"


# ── 验收 11 的前端判据来源 ──────────────────────────────────────────
def test_me_exposes_email_deliverable(client):
    account = {"username": "tester", "email": "1+u@noreply.gitcode.com", "nickname": "",
               "avatar_url": "", "password_hash": None}
    with patch("src.domains.mgmt.routes.grape_auth.get_grape_account_by_id", return_value=account), \
         patch("src.domains.mgmt.routes.grape_auth.get_user_identities", return_value=[]), \
         patch("src.domains.mgmt.routes.grape_auth.has_password", return_value=False):
        r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["email_deliverable"] is False


# ── K：生产环境邮件服务缺失 → fail-closed ──────────────────────────
def test_register_verify_fails_closed_without_email_config_in_production(client):
    with patch("src.domains.mgmt.auth.email_service.is_email_configured", return_value=False), \
         patch("src.domains.mgmt.auth.email_service.is_production_env", return_value=True):
        r = client.post("/api/auth/register/verify", json={
            "username": "newbie", "email": "newbie@example.com",
            "password": "TestPass123!", "code": "123456"})
    assert r.status_code == 503
    assert r.json()["detail"]["error_code"] == "EMAIL_NOT_CONFIGURED"