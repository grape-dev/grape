"""Issue #345 方向2：全局 bot token DB 迁移 + 统一入口测试。"""
import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def fresh_db():
    """每个测试用独立内存 SQLite。"""
    import src.session_manager as sm
    sm.db = MagicMock()
    sm.db_lock = MagicMock()
    sm.db.cursor.return_value = sm.db.cursor.return_value or MagicMock()
    # 模拟 meta 表读写
    _meta = {}
    cursor = MagicMock()
    def _execute(sql, params=()):
        sql_upper = sql.strip().upper()
        if sql_upper.startswith("SELECT VALUE FROM META"):
            key = params[0] if params else ""
            cursor.fetchone.return_value = (_meta.get(key, ""),) if key in _meta else None
        elif sql_upper.startswith("INSERT OR REPLACE INTO META"):
            key, val = params[0], params[1]
            _meta[key] = val
    cursor.execute = _execute
    sm.db.cursor.return_value = cursor
    return _meta, cursor


class TestGlobalGitcodeBotToken:
    """统一入口：DB meta 表读取 + 文件 fallback（60s 缓存）。"""

    def setup_method(self):
        from src.user.db import _invalidate_global_bot_cache
        _invalidate_global_bot_cache()

    def test_db_has_value(self, fresh_db):
        from src.user.db import get_global_gitcode_bot_token
        with patch("src.user.db.get_meta_value", return_value="db_token_123"):
            result = get_global_gitcode_bot_token()
        assert result == "db_token_123"

    def test_db_empty_fallback_to_file(self, fresh_db):
        from src.user.db import get_global_gitcode_bot_token
        with patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db._read_gbt_file_field", return_value="file_token_456"):
            result = get_global_gitcode_bot_token()
        assert result == "file_token_456"

    def test_both_empty_returns_empty(self, fresh_db):
        from src.user.db import get_global_gitcode_bot_token
        with patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db._read_gbt_file_field", return_value=""):
            result = get_global_gitcode_bot_token()
        assert result == ""

    def test_cache_hits_within_ttl(self):
        """60s 内只查一次 DB。"""
        from src.user.db import get_global_gitcode_bot_token
        with patch("src.user.db.get_meta_value", return_value="cached_tok") as mock_get:
            get_global_gitcode_bot_token()
            get_global_gitcode_bot_token()
            get_global_gitcode_bot_token()
            assert mock_get.call_count == 1  # 只查了一次 DB

    def test_cache_expires_after_ttl(self):
        """TTL 过期后重新查 DB。"""
        import time as _t
        from src.user.db import get_global_gitcode_bot_token, _global_bot_cache
        with patch("src.user.db.get_meta_value", return_value="first") as mock_get:
            get_global_gitcode_bot_token()
            # 模拟过期：把缓存时间戳改为过去
            for k in list(_global_bot_cache.keys()):
                _global_bot_cache[k] = (_global_bot_cache[k][0], _t.time() - 1)
            with patch("src.user.db.get_meta_value", return_value="second"):
                result = get_global_gitcode_bot_token()
            assert result == "second"
            assert mock_get.call_count == 1  # 第一次调用

    def test_cache_invalidation(self):
        """admin 改配置后清缓存，下次读走 DB。"""
        from src.user.db import get_global_gitcode_bot_token, _invalidate_global_bot_cache
        with patch("src.user.db.get_meta_value", return_value="old") as mock_get:
            get_global_gitcode_bot_token()
            _invalidate_global_bot_cache()
            with patch("src.user.db.get_meta_value", return_value="new"):
                result = get_global_gitcode_bot_token()
            assert result == "new"
            assert mock_get.call_count == 1  # 清缓存后又查了一次

    def test_no_file_fallback_after_migration(self):
        """迁移完成后（migrated=1），DB 空值不再 fallback 到文件。"""
        from src.user.db import get_global_gitcode_bot_token, _invalidate_global_bot_cache
        _invalidate_global_bot_cache()
        # 模拟迁移已完成 + DB 值为空
        _meta_values = {"global_bot_token_migrated": "1", "global_bot_gitcode_token": ""}
        with patch("src.user.db.get_meta_value", side_effect=lambda k: _meta_values.get(k, "")), \
             patch("src.user.db._read_gbt_file_field", return_value="file_old_value") as mock_file:
            result = get_global_gitcode_bot_token()
            assert result == ""  # DB 空值，不 fallback 到文件
            mock_file.assert_not_called()  # 文件 fallback 没被调用

    def test_file_fallback_before_migration(self):
        """迁移未完成时，DB 空值 fallback 到文件。"""
        from src.user.db import get_global_gitcode_bot_token, _invalidate_global_bot_cache
        _invalidate_global_bot_cache()
        # 模拟迁移未完成（无 migrated 标记）+ DB 值为空
        _meta_values = {"global_bot_token_migrated": "", "global_bot_gitcode_token": ""}
        with patch("src.user.db.get_meta_value", side_effect=lambda k: _meta_values.get(k, "")), \
             patch("src.user.db._read_gbt_file_field", return_value="file_old_value") as mock_file:
            result = get_global_gitcode_bot_token()
            assert result == "file_old_value"  # fallback 到文件
            mock_file.assert_called_once()


class TestGlobalGithubBotToken:
    def setup_method(self):
        from src.user.db import _invalidate_global_bot_cache
        _invalidate_global_bot_cache()

    def test_db_has_value(self):
        from src.user.db import get_global_github_bot_token
        with patch("src.user.db.get_meta_value", return_value="gh_db_token"):
            result = get_global_github_bot_token()
        assert result == "gh_db_token"

    def test_db_empty_fallback_to_file(self):
        from src.user.db import get_global_github_bot_token
        with patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db._read_gbt_file_field", return_value="gh_file_token"):
            result = get_global_github_bot_token()
        assert result == "gh_file_token"

    def test_both_empty_returns_empty(self):
        from src.user.db import get_global_github_bot_token
        with patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db._read_gbt_file_field", return_value=""):
            result = get_global_github_bot_token()
        assert result == ""


class TestGlobalBotUsernames:
    def setup_method(self):
        from src.user.db import _invalidate_global_bot_cache
        _invalidate_global_bot_cache()

    def test_db_has_value(self):
        from src.user.db import get_global_bot_usernames
        with patch("src.user.db.get_meta_value", return_value='["bot1","bot2"]'):
            result = get_global_bot_usernames()
        assert result == ["bot1", "bot2"]

    def test_db_empty_fallback_to_file(self):
        from src.user.db import get_global_bot_usernames
        with patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db._read_gbt_file_usernames", return_value='["fallback_bot"]'):
            result = get_global_bot_usernames()
        assert result == ["fallback_bot"]

    def test_both_empty_returns_empty(self):
        from src.user.db import get_global_bot_usernames
        with patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db._read_gbt_file_usernames", return_value=""):
            result = get_global_bot_usernames()
        assert result == []

    def test_db_invalid_json_returns_empty(self):
        from src.user.db import get_global_bot_usernames
        with patch("src.user.db.get_meta_value", return_value="invalid json"), \
             patch("src.user.db._read_gbt_file_usernames", return_value=""):
            result = get_global_bot_usernames()
            assert result == []


class TestGetGlobalBotCredential:
    def setup_method(self):
        from src.user.db import _invalidate_global_bot_cache
        _invalidate_global_bot_cache()

    def test_returns_token_and_username(self):
        from src.user.db import get_global_bot_credential
        with patch("src.user.db.get_global_gitcode_bot_token", return_value="tok"), \
             patch("src.user.db.get_global_bot_usernames", return_value=["bot_user"]):
            tok, user = get_global_bot_credential()
        assert tok == "tok"
        assert user == "bot_user"

    def test_empty_when_no_config(self):
        from src.user.db import get_global_bot_credential
        with patch("src.user.db.get_global_gitcode_bot_token", return_value=""), \
             patch("src.user.db.get_global_bot_usernames", return_value=[]):
            tok, user = get_global_bot_credential()
        assert tok == ""
        assert user == ""


class TestMigrateGlobalBotTokenToDb:
    """启动迁移逻辑测试。"""

    def test_skip_if_already_migrated(self):
        """DB 已有迁移标记时跳过迁移。"""
        with patch("src.lock.acquire_lock", return_value=True), \
             patch("src.user.db.get_meta_value", return_value="1"), \
             patch("src.user.db.set_meta_value") as mock_set:
            from src.api.db_setup import _migrate_global_bot_token_to_db
            _migrate_global_bot_token_to_db()
            mock_set.assert_not_called()

    def test_skip_if_lock_not_acquired(self):
        """其他实例持锁时跳过。"""
        with patch("src.lock.acquire_lock", return_value=False), \
             patch("src.user.db.get_meta_value") as mock_get:
            from src.api.db_setup import _migrate_global_bot_token_to_db
            _migrate_global_bot_token_to_db()
            mock_get.assert_not_called()

    def test_migrate_from_file(self, tmp_path):
        """文件有值、DB 无值时迁移。"""
        config_file = tmp_path / "global-bot-token.json"
        config_data = {
            "gitcode_api_token": "gc_tok_valid_token_1234567890",
            "github_bot_token": "gh_tok_valid_token_9876543210",
            "github_bot_username": "grapedev-bot",
            "bot_usernames": ["bot1"],
            "gitcode_webhook_token": "wh_tok",
            "webhook_url": "https://example.com/webhook",
        }
        config_file.write_text(json.dumps(config_data))

        set_calls = {}
        def mock_set(key, val):
            set_calls[key] = val

        with patch("src.lock.acquire_lock", return_value=True), \
             patch("src.lock.release_lock"), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db.set_meta_value", side_effect=mock_set), \
             patch("src.api.db_setup._CONFIG_DIR", str(tmp_path)):
            from src.api.db_setup import _migrate_global_bot_token_to_db
            _migrate_global_bot_token_to_db()

        assert set_calls.get("global_bot_gitcode_token") == "gc_tok_valid_token_1234567890"
        assert set_calls.get("global_bot_github_token") == "gh_tok_valid_token_9876543210"
        assert json.loads(set_calls.get("global_bot_usernames", "[]")) == ["bot1"]
        assert set_calls.get("gitcode_webhook_token") == "wh_tok"
        assert set_calls.get("webhook_url") == "https://example.com/webhook"
        assert set_calls.get("global_bot_token_migrated") == "1"

    def test_skip_placeholder_token(self, tmp_path):
        """占位符 token（含省略号/过短）不迁移。"""
        config_file = tmp_path / "global-bot-token.json"
        config_data = {
            "gitcode_api_token": "ghp_zG...uUef",  # 占位符
            "github_bot_token": "short",  # 过短
        }
        config_file.write_text(json.dumps(config_data))

        set_calls = {}
        def mock_set(key, val):
            set_calls[key] = val

        with patch("src.lock.acquire_lock", return_value=True), \
             patch("src.lock.release_lock"), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db.set_meta_value", side_effect=mock_set), \
             patch("src.api.db_setup._CONFIG_DIR", str(tmp_path)):
            from src.api.db_setup import _migrate_global_bot_token_to_db
            _migrate_global_bot_token_to_db()

        assert "global_bot_gitcode_token" not in set_calls  # 占位符没迁
        assert "global_bot_github_token" not in set_calls  # 过短没迁
        assert set_calls.get("global_bot_token_migrated") == "1"  # 迁移标记照写

    def test_db_has_value_skip_overwrite(self, tmp_path):
        """DB 已有值时不覆盖（admin 已配置的值不被文件旧值覆盖）。"""
        config_file = tmp_path / "global-bot-token.json"
        config_data = {
            "gitcode_api_token": "file_old_token_1234567890",
            "github_bot_token": "file_gh_old_token_9876543",
        }
        config_file.write_text(json.dumps(config_data))

        set_calls = {}
        def mock_set(key, val):
            set_calls[key] = val

        # DB 已有 gitcode token，没有 github token
        def mock_get(key):
            if key == "global_bot_gitcode_token":
                return "db_admin_set_token"
            if key == "global_bot_token_migrated":
                return ""  # 未迁移
            return ""

        with patch("src.lock.acquire_lock", return_value=True), \
             patch("src.lock.release_lock"), \
             patch("src.user.db.get_meta_value", side_effect=mock_get), \
             patch("src.user.db.set_meta_value", side_effect=mock_set), \
             patch("src.api.db_setup._CONFIG_DIR", str(tmp_path)):
            from src.api.db_setup import _migrate_global_bot_token_to_db
            _migrate_global_bot_token_to_db()

        # gitcode token: DB 有值 → 不覆盖
        assert "global_bot_gitcode_token" not in set_calls
        # github token: DB 无值 → 从文件迁移
        assert set_calls.get("global_bot_github_token") == "file_gh_old_token_9876543"

    def test_skip_if_no_file(self, tmp_path):
        """文件不存在时跳过。"""
        with patch("src.lock.acquire_lock", return_value=True), \
             patch("src.lock.release_lock"), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db.set_meta_value") as mock_set, \
             patch("src.api.db_setup._CONFIG_DIR", str(tmp_path)):
            from src.api.db_setup import _migrate_global_bot_token_to_db
            _migrate_global_bot_token_to_db()
            mock_set.assert_not_called()

    def test_lock_always_released(self):
        """异常时锁也要释放。"""
        with patch("src.lock.acquire_lock", return_value=True), \
             patch("src.lock.release_lock") as mock_release, \
             patch("src.user.db.get_meta_value", side_effect=Exception("DB error")):
            from src.api.db_setup import _migrate_global_bot_token_to_db
            try:
                _migrate_global_bot_token_to_db()
            except Exception:
                pass
            mock_release.assert_called_once_with("migrate_global_bot_token")
