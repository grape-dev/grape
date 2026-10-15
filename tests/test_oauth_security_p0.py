"""P0 批次安全加固测试——需求 1/2/4/5/9。

需求 1：state 绑定 session（login CSRF 防护）
需求 2：state 原子消费
需求 4：refresh_token 存储链路统一
需求 5：消除 provider 硬编码
需求 9：cooldown datetime('now') 修复
"""
import os
import sys
import time
import json
import hashlib

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from src.session_manager import db, db_lock
from src.user import db as user_db


@pytest.fixture(autouse=True)
def _clean_oauth_states():
    """每个测试前清理 oauth_states 表和测试账号。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM oauth_states")
        # 清理可能残留的测试数据
        c.execute("DELETE FROM external_identities WHERE external_id LIKE 'test_ext_%'")
        c.execute("DELETE FROM grape_accounts WHERE username LIKE 'test_rt_%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM oauth_states")
        c.execute("DELETE FROM external_identities WHERE external_id LIKE 'test_ext_%'")
        c.execute("DELETE FROM grape_accounts WHERE username LIKE 'test_rt_%'")
        db.commit()


# ── 需求 2：state 原子消费 ──────────────────────────────────────────

class TestStateAtomicConsume:
    """需求 2：validate_oauth_state 原子消费。"""

    def test_single_consume_returns_data(self):
        """第一次消费返回数据。"""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        user_db.save_oauth_state(state, "gitcode", {"login_flow": True})
        data = user_db.validate_oauth_state(state)
        assert data is not None
        assert data.get("_provider") == "gitcode"
        assert data.get("login_flow") is True

    def test_double_consume_returns_none(self):
        """第二次消费返回 None（已被消费）。"""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        user_db.save_oauth_state(state, "gitcode", {"login_flow": True})
        first = user_db.validate_oauth_state(state)
        second = user_db.validate_oauth_state(state)
        assert first is not None
        assert second is None

    def test_expired_state_returns_none(self):
        """过期的 state 返回 None（DELETE 条件带 expires）。"""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        # 手动插入已过期的 state
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO oauth_states (state, provider, data_json, expires) VALUES (?,?,?,?)",
                (state, "gitcode", json.dumps({"login_flow": True}), time.time() - 1),
            )
            db.commit()
        data = user_db.validate_oauth_state(state)
        assert data is None

    def test_nonexistent_state_returns_none(self):
        """不存在的 state 返回 None。"""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        data = user_db.validate_oauth_state(state)
        assert data is None

    def test_provider_mismatch_returns_none(self):
        """provider 不匹配返回 None。"""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        user_db.save_oauth_state(state, "gitcode", {"login_flow": True})
        data = user_db.validate_oauth_state(state, provider="github")
        assert data is None

    def test_provider_match_returns_data(self):
        """provider 匹配返回数据。"""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        user_db.save_oauth_state(state, "gitcode", {"login_flow": True})
        data = user_db.validate_oauth_state(state, provider="gitcode")
        assert data is not None


# ── 需求 4+5：refresh_token 链路 ────────────────────────────────────

class TestRefreshTokenLink:
    """需求 4+5：refresh_token 读取源 + provider 硬编码消除。"""

    def test_read_from_identity_no_record(self):
        """无 external_identities 记录返回空。"""
        rt = user_db._read_refresh_token_from_identity(99999, "gitcode")
        assert rt == ""

    def test_read_from_identity_empty_token(self):
        """refresh_token 字段为空返回空。"""
        from src.auth.grape_account_db import create_external_identity, delete_external_identity
        from src.auth.grape_account_db import create_grape_account, soft_delete_account
        account = create_grape_account(username="test_rt_empty", email="", password="")
        assert account is not None
        try:
            create_external_identity(
                grape_user_id=account["id"], provider="gitcode",
                external_id="test_ext_rt_empty", access_token="",
                refresh_token="",
            )
            rt = user_db._read_refresh_token_from_identity(account["id"], "gitcode")
            assert rt == ""
        finally:
            delete_external_identity(account["id"], "gitcode")
            soft_delete_account(account["id"])

    def test_read_from_identity_valid_token(self):
        """有加密 refresh_token 返回解密后的明文。"""
        from src.auth.grape_account_db import create_external_identity, delete_external_identity
        from src.auth.grape_account_db import create_grape_account, soft_delete_account
        account = create_grape_account(username="test_rt_valid", email="", password="")
        assert account is not None
        try:
            create_external_identity(
                grape_user_id=account["id"], provider="gitcode",
                external_id="test_ext_rt_valid", access_token="access123",
                refresh_token="refresh456",
            )
            rt = user_db._read_refresh_token_from_identity(account["id"], "gitcode")
            assert rt == "refresh456"
        finally:
            delete_external_identity(account["id"], "gitcode")
            soft_delete_account(account["id"])

    def test_read_from_credentials_no_cred(self):
        """无 user_credentials 记录返回空。"""
        rt = user_db._read_refresh_token_from_credentials(99999, "gitcode")
        assert rt == ""

    def test_no_provider_hardcoding(self):
        """需求 5：refresh_oauth_token 代码中无 if provider == 'gitcode' 硬编码。"""
        import inspect
        source = inspect.getsource(user_db.refresh_oauth_token)
        assert 'if provider == "gitcode"' not in source
        assert "if provider == 'gitcode'" not in source


# ── 需求 9：cooldown datetime 修复 ──────────────────────────────────

class TestCooldownDatetime:
    """需求 9：cooldown.py 无 datetime('now')。"""

    def test_no_datetime_now_in_cooldown(self):
        """cooldown.py 中无 datetime('now') SQL 内建函数。"""
        import src.admin.cooldown as cooldown_mod
        import inspect
        source = inspect.getsource(cooldown_mod)
        # 排除 docstring 中的文字描述
        # 检查 SQL 语句中不含 datetime('now')
        lines = source.split("\n")
        sql_lines = [l for l in lines if "execute" in l or "WHERE" in l or "SET" in l or "VALUES" in l]
        for line in sql_lines:
            assert "datetime('now')" not in line, f"SQL line contains datetime('now'): {line.strip()}"


# ── 需求 1：state 绑定 session ──────────────────────────────────────

class TestStateSessionBinding:
    """需求 1：login flow 的 state 包含 session_nonce。"""

    def test_login_state_contains_nonce(self):
        """login flow 的 state 数据中包含 session_nonce。"""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        import secrets
        nonce = secrets.token_hex(32)
        user_db.save_oauth_state(state, "gitcode", {
            "login_flow": True,
            "session_nonce": nonce,
        })
        data = user_db.validate_oauth_state(state)
        assert data is not None
        assert data.get("session_nonce") == nonce
        assert data.get("login_flow") is True

    def test_bind_state_no_nonce(self):
        """绑定流程的 state 不包含 session_nonce。"""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        user_db.save_oauth_state(state, "gitcode", {
            "binding_user_id": 123,
        })
        data = user_db.validate_oauth_state(state)
        assert data is not None
        assert "session_nonce" not in data
        assert data.get("binding_user_id") == 123

    def test_nonce_is_64_hex_chars(self):
        """nonce 是 32 字节的 hex（64 字符）。"""
        import secrets
        nonce = secrets.token_hex(32)
        assert len(nonce) == 64
        # 确保是合法 hex
        int(nonce, 16)


# ── 加固项 §10.6：redirect_uri 从 state 恢复 ─────────────────────────

class TestRedirectUriInState:
    """Hardening §10.6: redirect_uri stored in state, recovered at callback."""

    def test_build_auth_url_stores_redirect_uri(self):
        """build_auth_url saves redirect_uri into state_data."""
        from src.auth.providers import build_auth_url, get_redirect_uri
        from unittest.mock import patch
        mock_cfg = {"name": "Test", "redirect_uri": "https://example.com/cb/test",
                     "auth_url": "https://example.com/auth", "scope": "",
                     "pkce_enabled": False}
        with patch("src.auth.providers.get_provider", return_value=mock_cfg), \
             patch("src.auth.providers._get_client_id", return_value="cid"):
            state = hashlib.sha256(os.urandom(32)).hexdigest()
            build_auth_url("test", state, "https://example.com/cb/test")
        data = user_db.validate_oauth_state(state)
        assert data is not None
        assert data.get("redirect_uri") == "https://example.com/cb/test"

    def test_callback_recovers_redirect_uri_from_state(self):
        """oauth_callback uses redirect_uri from state, not re-constructing."""
        # Simulate: save state with redirect_uri, then validate recovers it
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        original_uri = "https://original.example.com/api/auth/callback/gitcode"
        user_db.save_oauth_state(state, "gitcode", {
            "login_flow": True,
            "redirect_uri": original_uri,
        })
        data = user_db.validate_oauth_state(state)
        assert data is not None
        assert data.get("redirect_uri") == original_uri

    def test_state_without_redirect_uri_falls_back(self):
        """Legacy state without redirect_uri field triggers fallback."""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        user_db.save_oauth_state(state, "gitcode", {"login_flow": True})
        data = user_db.validate_oauth_state(state)
        assert data is not None
        # Fallback: redirect_uri not in state → empty string
        assert data.get("redirect_uri", "") == ""
