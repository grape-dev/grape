"""P1/P2 批次安全加固测试——需求 3/6/7/8/10/11。

需求 3：回调恢复分路径
需求 6：占位账号合并同事务
需求 7：合并账号邮箱清理
需求 8：冷静期匿名化单事务
需求 10：解绑 placeholder 邮箱走数据访问层
需求 11：redirect_uri 从 provider 配置读取
"""
import os
import sys
import time
import json
import hashlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from src.session_manager import db, db_lock
from src.user import db as user_db


@pytest.fixture(autouse=True)
def _clean_test_data():
    """每个测试前清理测试数据。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM oauth_states")
        c.execute("DELETE FROM external_identities WHERE external_id LIKE 'test_p12_%'")
        c.execute("DELETE FROM user_credentials WHERE user_id IN "
                  "(SELECT id FROM grape_accounts WHERE username LIKE 'test_p12_%')")
        c.execute("DELETE FROM grape_accounts WHERE username LIKE 'test_p12_%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM oauth_states")
        c.execute("DELETE FROM external_identities WHERE external_id LIKE 'test_p12_%'")
        c.execute("DELETE FROM user_credentials WHERE user_id IN "
                  "(SELECT id FROM grape_accounts WHERE username LIKE 'test_p12_%')")
        c.execute("DELETE FROM grape_accounts WHERE username LIKE 'test_p12_%'")
        db.commit()


def _set_email_fields(user_id: int, email: str, email_source: str, is_placeholder: int):
    """直接设置 grape_accounts 的 email 相关字段（测试辅助）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_accounts SET email=?, email_source=?, email_is_placeholder=? WHERE id=?",
            (email, email_source, is_placeholder, user_id),
        )
        db.commit()


# ── 需求 3：回调恢复分路径 ──────────────────────────────────────────

class TestCallbackSplitPath:
    """需求 3：回调路由从统一 /api/auth/oauth/callback 改为分路径 /api/auth/callback/{provider}。"""

    def test_callback_route_exists_with_provider(self):
        """分路径路由 /api/auth/callback/gitcode 存在且可访问。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        client = TestClient(app)
        r = client.get("/api/auth/callback/gitcode", params={"state": "bad", "code": "c"},
                       follow_redirects=False)
        assert r.status_code == 302

    def test_old_unified_route_deleted(self):
        """旧统一路由 /api/auth/oauth/callback 已删除（404）。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        client = TestClient(app)
        r = client.get("/api/auth/oauth/callback", params={"state": "x", "code": "c"},
                       follow_redirects=False)
        assert r.status_code == 404

    def test_provider_mismatch_rejected(self):
        """URL 路径中的 provider 与 state 中的 provider 不一致时拒绝。"""
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        user_db.save_oauth_state(state, "gitcode", {"login_flow": True})
        from fastapi.testclient import TestClient
        from src.api.main import app
        client = TestClient(app)
        r = client.get("/api/auth/callback/github", params={"state": state, "code": "c"},
                       follow_redirects=False)
        assert r.status_code == 302
        assert "provider_mismatch" in r.headers.get("location", "") or "oauth_error" in r.headers.get("location", "")


# ── 需求 6：占位账号合并同事务 ──────────────────────────────────────

class TestMergeSameTransaction:
    """需求 6：unbind → create → mark status=4 在同一个 with db_lock 事务内。"""

    def test_unbind_placeholder_binding(self):
        """unbind_placeholder_binding 将旧绑定标记为 status=0。"""
        from src.auth.grape_account_db import (
            create_grape_account, create_external_identity,
            unbind_placeholder_binding, get_external_identity_any_status,
        )
        account = create_grape_account(username="test_p12_unbind", email="", password="")
        assert account is not None
        create_external_identity(
            grape_user_id=account["id"], provider="gitcode",
            external_id="test_p12_unbind_ext", access_token="at", refresh_token="rt",
        )
        with db_lock:
            c = db.cursor()
            unbind_placeholder_binding(c, account["id"], "gitcode")
            db.commit()
        ident = get_external_identity_any_status("gitcode", "test_p12_unbind_ext")
        assert ident is not None
        assert ident["status"] == 0

    def test_mark_account_merged_clears_email(self):
        """mark_account_merged 标记 status=4 + 清空邮箱。"""
        from src.auth.grape_account_db import (
            create_grape_account_oauth, mark_account_merged, get_grape_account_by_id,
        )
        account = create_grape_account_oauth(
            username="test_p12_merge", email="noreply@noreply.gitcode.com",
            provider="gitcode",
        )
        assert account is not None
        with db_lock:
            c = db.cursor()
            mark_account_merged(c, account["id"])
            db.commit()
        acc = get_grape_account_by_id(account["id"])
        assert acc is not None
        assert acc["status"] == 4
        assert not acc["email"]  # NULL → "" by _row_to_account
        assert acc["email_source"] is None
        assert acc["email_is_placeholder"] == 0
        assert acc["username"].startswith("merged_")

    def test_restore_binding_in_tx_revives_status0(self):
        """restore_binding_in_tx 恢复 status=0 的记录（只写不读，MySQL 安全）。"""
        from src.auth.grape_account_db import (
            create_grape_account, create_external_identity,
            unbind_placeholder_binding, restore_binding_in_tx,
            mark_account_merged, get_external_identity,
        )
        placeholder = create_grape_account(username="test_p12_placeholder", email="", password="")
        assert placeholder is not None
        create_external_identity(
            grape_user_id=placeholder["id"], provider="gitcode",
            external_id="test_p12_create_tx", access_token="old_at", refresh_token="old_rt",
        )
        target = create_grape_account(username="test_p12_target", email="", password="")
        assert target is not None

        with db_lock:
            c = db.cursor()
            unbind_placeholder_binding(c, placeholder["id"], "gitcode")
            restore_binding_in_tx(
                c, old_user_id=placeholder["id"], new_user_id=target["id"],
                provider="gitcode", external_id="test_p12_create_tx",
                access_token="new_at", refresh_token="new_rt",
            )
            mark_account_merged(c, placeholder["id"])
            db.commit()

        ident = get_external_identity("gitcode", "test_p12_create_tx")
        assert ident is not None
        assert ident["grape_user_id"] == target["id"]

    def test_merge_placeholder_account_deprecated_wrapper(self):
        """旧 merge_placeholder_account 兼容包装仍然可用（含邮箱清理）。"""
        from src.auth.grape_account_db import (
            create_grape_account_oauth, create_external_identity,
            merge_placeholder_account, get_grape_account_by_id,
            create_grape_account,
        )
        placeholder = create_grape_account_oauth(
            username="test_p12_deprecated", email="noreply@noreply.gitcode.com",
            provider="gitcode",
        )
        assert placeholder is not None
        create_external_identity(
            grape_user_id=placeholder["id"], provider="gitcode",
            external_id="test_p12_dep_ext", access_token="at", refresh_token="rt",
        )
        target = create_grape_account(username="test_p12_dep_target", email="", password="")
        assert target is not None
        ok = merge_placeholder_account(placeholder["id"], target["id"], "gitcode", "test_p12_dep_ext")
        assert ok is True
        acc = get_grape_account_by_id(placeholder["id"])
        assert acc is not None
        assert acc["status"] == 4
        assert not acc["email"]  # NULL → "" by _row_to_account


# ── 需求 7：合并账号邮箱清理 ────────────────────────────────────────

class TestMergedAccountEmailCleanup:
    """需求 7：status=4 账号邮箱清空，释放唯一索引。"""

    def test_mark_account_merged_clears_all_email_fields(self):
        """mark_account_merged 清空 email/email_source/email_is_placeholder。"""
        from src.auth.grape_account_db import (
            create_grape_account_oauth, mark_account_merged, get_grape_account_by_id,
        )
        account = create_grape_account_oauth(
            username="test_p12_email_clear", email="noreply@noreply.gitcode.com",
            provider="gitcode",
        )
        assert account is not None
        with db_lock:
            c = db.cursor()
            mark_account_merged(c, account["id"])
            db.commit()
        acc = get_grape_account_by_id(account["id"])
        assert acc is not None
        assert not acc["email"]  # NULL → "" by _row_to_account
        assert acc["email_source"] is None
        assert acc["email_is_placeholder"] == 0

    def test_transfer_bind_clears_email(self):
        """transfer_bind 中 status=4 SQL 包含 email 清理。"""
        from src.auth.grape_account_db import create_grape_account, get_grape_account_by_id
        from src.session_manager import db as _db, db_lock as _dl
        account = create_grape_account(
            username="test_p12_transfer", email="transfer@example.com", password="",
        )
        assert account is not None
        _set_email_fields(account["id"], "noreply@noreply.gitcode.com", "gitcode", 1)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with _dl:
            c = _db.cursor()
            c.execute(
                "UPDATE grape_accounts SET status=4, username='merged_' || CAST(id AS TEXT), "
                "email=NULL, email_source=NULL, email_is_placeholder=0, "
                "updated_at=? WHERE id=?",
                (now, account["id"]),
            )
            _db.commit()
        acc = get_grape_account_by_id(account["id"])
        assert acc is not None
        assert acc["status"] == 4
        assert not acc["email"]  # NULL → "" by _row_to_account


# ── 需求 8：冷静期匿名化单事务 ──────────────────────────────────────

class TestCooldownSingleTransaction:
    """需求 8：_anonymize_account Step 1-4 在同一个事务内。"""

    def test_anonymize_success(self):
        """匿名化成功：status=2 + 字段匿名化。"""
        from src.auth.grape_account_db import create_grape_account, get_grape_account_by_id
        from src.admin.cooldown import _anonymize_account
        account = create_grape_account(
            username="test_p12_anon", email="anon@test.com", password="pw",
            nickname="TestUser", avatar_url="http://example.com/a.png",
        )
        assert account is not None
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE grape_accounts SET status=3 WHERE id=?", (account["id"],))
            db.commit()
        _anonymize_account(account["id"])
        acc = get_grape_account_by_id(account["id"])
        assert acc is not None
        assert acc["status"] == 2
        assert not acc["email"]  # NULL → "" by _row_to_account
        assert acc["password_hash"] == ""
        assert acc["username"] == f"deleted_{account['id']}"

    def test_anonymize_idempotent(self):
        """匿名化幂等：已处理（status=2）的账号再次执行是 no-op。"""
        from src.auth.grape_account_db import create_grape_account, get_grape_account_by_id
        from src.admin.cooldown import _anonymize_account
        account = create_grape_account(username="test_p12_idem", email="idem@test.com", password="pw")
        assert account is not None
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE grape_accounts SET status=3 WHERE id=?", (account["id"],))
            db.commit()
        _anonymize_account(account["id"])
        _anonymize_account(account["id"])
        acc = get_grape_account_by_id(account["id"])
        assert acc is not None
        assert acc["status"] == 2

    def test_anonymize_clears_external_identities(self):
        """匿名化清除 external_identities。"""
        from src.auth.grape_account_db import (
            create_grape_account, create_external_identity, get_external_identity,
        )
        from src.admin.cooldown import _anonymize_account
        account = create_grape_account(username="test_p12_ext", email="", password="")
        assert account is not None
        create_external_identity(
            grape_user_id=account["id"], provider="gitcode",
            external_id="test_p12_ext_id", access_token="at", refresh_token="rt",
        )
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE grape_accounts SET status=3 WHERE id=?", (account["id"],))
            db.commit()
        _anonymize_account(account["id"])
        ident = get_external_identity("gitcode", "test_p12_ext_id")
        assert ident is None

    def test_no_datetime_now_in_anonymize(self):
        """_anonymize_account 源码中无 datetime('now') SQL。"""
        import inspect
        from src.admin.cooldown import _anonymize_account
        source = inspect.getsource(_anonymize_account)
        sql_lines = [l for l in source.split("\n") if "execute" in l or "WHERE" in l or "SET" in l]
        for line in sql_lines:
            assert "datetime('now')" not in line, f"SQL contains datetime('now'): {line.strip()}"


# ── 需求 10：解绑 placeholder 邮箱走数据访问层 ──────────────────────

class TestUnbindViaDAL:
    """需求 10：解绑时 placeholder 邮箱清理通过 clear_placeholder_email 数据访问层。"""

    def test_clear_placeholder_email_clears(self):
        """placeholder 邮箱被清理。"""
        from src.auth.grape_account_db import (
            create_grape_account, clear_placeholder_email, get_grape_account_by_id,
        )
        account = create_grape_account(username="test_p12_clear_ph", email="", password="")
        assert account is not None
        _set_email_fields(account["id"], "noreply@noreply.gitcode.com", "gitcode", 1)
        clear_placeholder_email(account["id"], "gitcode")
        acc = get_grape_account_by_id(account["id"])
        assert acc is not None
        assert not acc["email"]  # NULL → "" by _row_to_account
        assert acc["email_source"] is None
        assert acc["email_is_placeholder"] == 0

    def test_clear_placeholder_email_not_placeholder(self):
        """非 placeholder 邮箱不受影响。"""
        from src.auth.grape_account_db import (
            create_grape_account, clear_placeholder_email, get_grape_account_by_id,
        )
        account = create_grape_account(
            username="test_p12_real_email", email="real@example.com", password="",
        )
        assert account is not None
        clear_placeholder_email(account["id"], "gitcode")
        acc = get_grape_account_by_id(account["id"])
        assert acc is not None
        assert acc["email"] == "real@example.com"

    def test_clear_placeholder_email_wrong_provider(self):
        """email_source 不匹配 provider 时不动。"""
        from src.auth.grape_account_db import (
            create_grape_account, clear_placeholder_email, get_grape_account_by_id,
        )
        account = create_grape_account(username="test_p12_wrong_prov", email="", password="")
        assert account is not None
        _set_email_fields(account["id"], "noreply@noreply.gitcode.com", "gitcode", 1)
        clear_placeholder_email(account["id"], "github")
        acc = get_grape_account_by_id(account["id"])
        assert acc is not None
        assert acc["email"] == "noreply@noreply.gitcode.com"

    def test_unbind_provider_no_direct_sql(self):
        """unbind_provider 源码中无直接 SQL 操作 grape_accounts 表。"""
        import inspect
        from src.api.routes.grape_auth import unbind_provider
        source = inspect.getsource(unbind_provider)
        assert "UPDATE grape_accounts" not in source
        assert "INSERT INTO grape_accounts" not in source
        assert "DELETE FROM grape_accounts" not in source


# ── 需求 11：redirect_uri 从 provider 配置读取 ──────────────────────

class TestRedirectUriFromProviderConfig:
    """需求 11：redirect_uri 从 provider 配置读取，不从环境变量。"""

    def test_redirect_uri_from_provider_config(self):
        """provider 配置有 redirect_uri 时直接返回。"""
        from src.auth.providers import get_redirect_uri
        from unittest.mock import patch
        mock_cfg = {"name": "TestProvider", "redirect_uri": "https://example.com/cb/test"}
        with patch("src.auth.providers.get_provider", return_value=mock_cfg):
            class FakeRequest:
                headers = {"Host": "localhost"}
            result = get_redirect_uri(FakeRequest(), "test")
        assert result == "https://example.com/cb/test"

    def test_redirect_uri_fallback_host_header(self):
        """provider 配置无 redirect_uri 时从 Host header 构造。"""
        from src.auth.providers import get_redirect_uri
        from unittest.mock import patch
        mock_cfg = {"name": "TestProvider"}
        with patch("src.auth.providers.get_provider", return_value=mock_cfg):
            class FakeRequest:
                headers = {"Host": "localhost:8000", "X-Forwarded-Proto": "https"}
            result = get_redirect_uri(FakeRequest(), "test")
        assert result == "https://localhost:8000/api/auth/callback/test"

    def test_redirect_uri_empty_provider_raises(self):
        """provider 为空字符串时抛 ValueError（统一回调路由已删除）。"""
        from src.auth.providers import get_redirect_uri
        import pytest as _pytest
        class FakeRequest:
            headers = {"Host": "localhost"}
        with _pytest.raises(ValueError, match="non-empty provider"):
            get_redirect_uri(FakeRequest(), "")

    def test_no_oauth_redirect_uri_env_var(self):
        """get_redirect_uri 代码中无 OAUTH_REDIRECT_URI 环境变量引用。"""
        import inspect
        from src.auth import providers
        source = inspect.getsource(providers.get_redirect_uri)
        assert "OAUTH_REDIRECT_URI" not in source

    def test_no_get_oauth_redirect_uri_function(self):
        """security_config.py 中无 get_oauth_redirect_uri 函数。"""
        from src.auth import security_config
        assert not hasattr(security_config, "get_oauth_redirect_uri")

    def test_example_config_has_redirect_uri(self):
        """oauth-providers.example.json 中每个 provider 有 redirect_uri。"""
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "configs", "oauth-providers.example.json"
        )
        with open(config_path) as f:
            data = json.load(f)
        for provider, cfg in data.items():
            assert "redirect_uri" in cfg, f"Provider {provider} missing redirect_uri"
            assert cfg["redirect_uri"].startswith("https://")
            assert f"/api/auth/callback/{provider}" in cfg["redirect_uri"]
