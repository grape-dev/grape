"""Issue #69: API Key 生命周期管理端点测试.

覆盖：
- POST /api/settings/api-keys  → 生成 key（明文仅返回一次，DB 存 sha256）
- GET  /api/settings/api-keys  → 列表（不含明文）
- DELETE /api/settings/api-keys/{id} → 吊销（软删除，立即失效）
- GET /api/auth/profile with grape_ token → 返回用户信息
- 吊销后 grape_ token → 401
- JWT 会话仍正常工作（回归）
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.jwt_utils import create_access_token
from src.api import auth_token
from src.user import db as user_db
from src.user.db import get_token_version
from src.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _clean_token_tables():
    """每用例前清空 token 表，防跨运行残留。"""
    with db_lock:
        db.cursor().execute("DELETE FROM api_tokens WHERE token LIKE 'test_%' OR token LIKE 'tk_test%' OR user_id IN (SELECT id FROM user_accounts WHERE username LIKE 'test_%' OR username LIKE 'tc_%')")
        db.cursor().execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
        db.commit()
    yield


def _make_user(username: str = "tester69") -> dict:
    return user_db.create_user(f"{username}_{uuid.uuid4().hex[:8]}", "pw")


def _jwt_token(user: dict) -> str:
    return create_access_token(user["id"], user["username"], get_token_version(user["id"]))


client = TestClient(app)


# ==================== POST: 生成 API Key ====================

def test_create_api_key_returns_plaintext_once():
    """POST /api/settings/api-keys 返回明文 key，前缀 grape_。"""
    user = _make_user()
    resp = client.post(
        "/api/settings/api-keys",
        json={"name": "CI Pipeline"},
        headers={"Authorization": f"Bearer {_jwt_token(user)}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["key"].startswith("grape_")
    assert data["name"] == "CI Pipeline"
    assert "id" in data


def test_create_api_key_empty_name_rejected():
    """空名称 → 400。"""
    user = _make_user()
    resp = client.post(
        "/api/settings/api-keys",
        json={"name": ""},
        headers={"Authorization": f"Bearer {_jwt_token(user)}"},
    )
    assert resp.status_code == 400


def test_create_api_key_no_auth_rejected():
    """无认证 → 401。"""
    resp = client.post("/api/settings/api-keys", json={"name": "test"})
    assert resp.status_code == 401


# ==================== GET: 列表 ====================

def test_list_api_keys_no_plaintext():
    """GET /api/settings/api-keys 列表不含明文 key。"""
    user = _make_user()
    # 先创建 2 个 key
    for name in ["key-a", "key-b"]:
        client.post(
            "/api/settings/api-keys",
            json={"name": name},
            headers={"Authorization": f"Bearer {_jwt_token(user)}"},
        )
    resp = client.get(
        "/api/settings/api-keys",
        headers={"Authorization": f"Bearer {_jwt_token(user)}"},
    )
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert len(keys) == 2
    for k in keys:
        assert "key" not in k  # 列表不含明文
        assert "token" not in k
        assert "sha256" not in k
        assert "name" in k
        assert "id" in k


def test_list_api_keys_user_isolation():
    """用户 A 看不到用户 B 的 key。"""
    user_a = _make_user("userA")
    user_b = _make_user("userB")
    client.post(
        "/api/settings/api-keys",
        json={"name": "key-a"},
        headers={"Authorization": f"Bearer {_jwt_token(user_a)}"},
    )
    resp = client.get(
        "/api/settings/api-keys",
        headers={"Authorization": f"Bearer {_jwt_token(user_b)}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["keys"]) == 0


# ==================== DELETE: 吊销 ====================

def test_revoke_api_key_immediate_invalidation():
    """吊销后 grape_ token 立即返回 401。"""
    user = _make_user()
    # 生成 key
    create_resp = client.post(
        "/api/settings/api-keys",
        json={"name": "to-revoke"},
        headers={"Authorization": f"Bearer {_jwt_token(user)}"},
    )
    plaintext = create_resp.json()["key"]
    key_id = create_resp.json()["id"]

    # 用 grape_ key 访问 profile → 成功
    profile_resp = client.get(
        "/api/auth/profile",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert profile_resp.status_code == 200
    assert profile_resp.json()["username"] == user["username"]

    # 吊销
    revoke_resp = client.delete(
        f"/api/settings/api-keys/{key_id}",
        headers={"Authorization": f"Bearer {_jwt_token(user)}"},
    )
    assert revoke_resp.status_code == 200

    # 吊销后 grape_ key → 401
    revoked_resp = client.get(
        "/api/auth/profile",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert revoked_resp.status_code == 401


def test_revoke_nonexistent_key_returns_404():
    """吊销不存在的 key → 404。"""
    user = _make_user()
    resp = client.delete(
        "/api/settings/api-keys/99999",
        headers={"Authorization": f"Bearer {_jwt_token(user)}"},
    )
    assert resp.status_code == 404


def test_revoke_other_users_key_returns_404():
    """用户 A 吊销用户 B 的 key → 404（不泄露存在性）。"""
    user_a = _make_user("ownerA")
    user_b = _make_user("ownerB")
    # 用户 B 创建 key
    create_resp = client.post(
        "/api/settings/api-keys",
        json={"name": "b-key"},
        headers={"Authorization": f"Bearer {_jwt_token(user_b)}"},
    )
    key_id = create_resp.json()["id"]
    # 用户 A 尝试吊销
    resp = client.delete(
        f"/api/settings/api-keys/{key_id}",
        headers={"Authorization": f"Bearer {_jwt_token(user_a)}"},
    )
    assert resp.status_code == 404


# ==================== /api/auth/profile with grape_ token ====================

def test_profile_with_api_key():
    """grape_ API Key 访问 /api/auth/profile 返回用户信息。"""
    user = _make_user()
    create_resp = client.post(
        "/api/settings/api-keys",
        json={"name": "profile-test"},
        headers={"Authorization": f"Bearer {_jwt_token(user)}"},
    )
    plaintext = create_resp.json()["key"]

    resp = client.get(
        "/api/auth/profile",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == user["username"]


def test_profile_with_jwt_regression():
    """JWT 会话访问 /api/auth/profile 仍正常（回归）。"""
    user = _make_user()
    resp = client.get(
        "/api/auth/profile",
        headers={"Authorization": f"Bearer {_jwt_token(user)}"},
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == user["username"]


def test_profile_with_invalid_token_returns_401():
    """无效 token → 401。"""
    resp = client.get(
        "/api/auth/profile",
        headers={"Authorization": "Bearer grape_invalid_fake_token_xxx"},
    )
    assert resp.status_code == 401


# ==================== 明文不入库 ====================

def test_plaintext_not_in_database():
    """DB 中只存 sha256，不存明文。"""
    user = _make_user()
    create_resp = client.post(
        "/api/settings/api-keys",
        json={"name": "no-plaintext"},
        headers={"Authorization": f"Bearer {_jwt_token(user)}"},
    )
    plaintext = create_resp.json()["key"]

    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_sha256, name FROM api_tokens WHERE user_id=?", (user["id"],))
        rows = c.fetchall()

    assert len(rows) == 1
    stored_hash = rows[0][0]
    # 明文不应直接出现在 DB 中
    assert plaintext not in stored_hash
    assert plaintext not in str(rows)
    # 验证存的是 sha256
    import hashlib
    assert stored_hash == hashlib.sha256(plaintext.encode()).hexdigest()
