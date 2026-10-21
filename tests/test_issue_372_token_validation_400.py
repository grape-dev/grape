"""Issue #372 §28: token 验证失败 401→400 消除前端 refresh 循环测试

settings.py GitHub/GitCode token 验证失败时用 401 → 前端 handleResp
误判 JWT 过期 → doRefresh → refresh 200 → 重发 → 业务 401 → 无限循环。
修复：改用 400 + error_code=TOKEN_INVALID。

测试覆盖：
- T15-01: GitHub token 验证失败返回 400（非 401）+ error_code=TOKEN_INVALID
- T15-02: GitCode token 验证失败返回 400（非 401）+ error_code=TOKEN_INVALID
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import json
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


def _get_client():
    """获取 TestClient（确保 app 已初始化）。"""
    from src.api.main import app
    return TestClient(app)


def _create_test_user(client):
    """创建测试用户并登录，返回 auth headers。"""
    from src.auth.grape_account_db import create_grape_account
    import uuid

    username = "test_tokval_" + uuid.uuid4().hex[:6]
    account = create_grape_account(username, f"{username}@test.com", "TestPass123!")
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_status_and_token_version
    _status, _tv, _uname = get_status_and_token_version(account["id"])
    token = create_access_token(account["id"], username, _tv)
    return {"Authorization": f"Bearer {token}"}, account["id"], username


def _cleanup_user(user_id):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (user_id,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (user_id,))
        db.commit()


def test_github_token_validation_returns_400():
    """T15-01: GitHub token 验证失败返回 400（非 401）+ error_code=TOKEN_INVALID。

    修复前：status_code=401 → 前端 handleResp 触发 doRefresh → 无限循环。
    修复后：status_code=400 + error_code=TOKEN_INVALID → 前端不触发 refresh。
    """
    client = _get_client()
    headers, uid, username = _create_test_user(client)
    try:
        # Mock requests.get to return 401 (GitHub token invalid)
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"message": "Bad credentials"}

        with patch("requests.get", return_value=mock_resp):
            resp = client.put(
                "/api/settings/credentials",
                json={"github_token": "invalid_token_12345"},
                headers=headers,
            )

        assert resp.status_code == 400, (
            f"GitHub token 验证失败应返回 400（非 401），实际: {resp.status_code}"
        )
        detail = resp.json().get("detail", {})
        assert detail.get("error_code") == "TOKEN_INVALID", (
            f"error_code 应为 TOKEN_INVALID，实际: {detail.get('error_code')}"
        )
        assert "token" in detail.get("message", "").lower(), (
            f"message 应含 'token'，实际: {detail.get('message')}"
        )
    finally:
        _cleanup_user(uid)


def test_gitcode_token_validation_returns_400():
    """T15-02: GitCode token 验证失败返回 400（非 401）+ error_code=TOKEN_INVALID。"""
    client = _get_client()
    headers, uid, username = _create_test_user(client)
    try:
        # Mock requests.get to return 401 (GitCode token invalid)
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"message": "Unauthorized"}

        with patch("requests.get", return_value=mock_resp):
            resp = client.put(
                "/api/settings/credentials",
                json={"gitcode_token": "invalid_gc_token_12345"},
                headers=headers,
            )

        assert resp.status_code == 400, (
            f"GitCode token 验证失败应返回 400（非 401），实际: {resp.status_code}"
        )
        detail = resp.json().get("detail", {})
        assert detail.get("error_code") == "TOKEN_INVALID", (
            f"error_code 应为 TOKEN_INVALID，实际: {detail.get('error_code')}"
        )
    finally:
        _cleanup_user(uid)


def test_token_invalid_i18n_key_exists():
    """T15-03: TOKEN_INVALID i18n key 在 en/zh locale 文件中都存在。"""
    import json as _json

    en_path = os.path.join(ROOT, "src/api/static/locales/en/app.json")
    zh_path = os.path.join(ROOT, "src/api/static/locales/zh/app.json")

    with open(en_path, encoding="utf-8") as f:
        en = _json.load(f)
    with open(zh_path, encoding="utf-8") as f:
        zh = _json.load(f)

    assert "error" in en and "TOKEN_INVALID" in en["error"], (
        "en/app.json error.TOKEN_INVALID must exist"
    )
    assert "error" in zh and "TOKEN_INVALID" in zh["error"], (
        "zh/app.json error.TOKEN_INVALID must exist"
    )
    assert en["error"]["TOKEN_INVALID"], "en TOKEN_INVALID must be non-empty"
    assert zh["error"]["TOKEN_INVALID"], "zh TOKEN_INVALID must be non-empty"
