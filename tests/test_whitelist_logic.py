"""Issue #333: 登录白名单全局开关逻辑测试。

全部 DB 操作用 MagicMock + unittest.mock.patch，不依赖真实数据库连接。
patch 目标是 src.config 内部已绑定的引用（from src.session_manager import db），
确保不污染其他测试的模块缓存。
"""
import pytest
from unittest.mock import MagicMock, patch


def _reset_whitelist_cache():
    """Reset whitelist cache between tests."""
    import src.config as cfg
    cfg._WL_ENABLED_CACHE = None
    cfg._WL_CACHE_TS = 0.0


def _make_mock_db(table_count=0, whitelist_rows=None):
    """创建 mock db + db_lock + get_meta_value，返回 patch context managers。"""
    mock_cursor = MagicMock()

    def _execute(sql, params=()):
        sql_upper = sql.upper().strip()
        if "COUNT(*)" in sql_upper:
            mock_cursor.fetchone.return_value = (table_count,)
            return mock_cursor
        elif "SELECT 1 FROM LOGIN_WHITELIST" in sql_upper:
            mock_cursor.fetchone.return_value = whitelist_rows
            return mock_cursor
        mock_cursor.fetchone.return_value = None
        return mock_cursor

    mock_cursor.execute = MagicMock(side_effect=_execute)
    mock_db = MagicMock()
    mock_db.cursor.return_value = mock_cursor

    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(return_value=None)
    mock_lock.__exit__ = MagicMock(return_value=False)

    return mock_db, mock_lock


def _run_is_user_allowed(meta_value, table_count, whitelist_rows, column, login):
    """Patch all dependencies and run is_user_allowed in isolation."""
    _reset_whitelist_cache()
    mock_db, mock_lock = _make_mock_db(table_count, whitelist_rows)

    # Patch src.config's internal references (already imported at module level)
    # src.config does: from src.session_manager import db, db_lock (inside function)
    # and: from src.user.db import get_meta_value (inside function)
    # So we patch at the source module level
    with patch('src.session_manager.db', mock_db), \
         patch('src.session_manager.db_lock', mock_lock), \
         patch('src.user.db.get_meta_value', return_value=meta_value):
        _reset_whitelist_cache()
        from src.config import is_user_allowed
        return is_user_allowed(column, login)


def test_whitelist_disabled_allows_all():
    """meta unset + empty table -> allow all."""
    result = _run_is_user_allowed("", 0, None, "gitcode_username", "anyuser")
    assert result is True


def test_whitelist_enabled_blocks_non_whitelisted():
    """meta=true + login not in table -> deny."""
    result = _run_is_user_allowed("true", 1, None, "gitcode_username", "bob")
    assert result is False


def test_whitelist_enabled_allows_whitelisted():
    """meta=true + login in table -> allow."""
    result = _run_is_user_allowed("true", 1, (1,), "gitcode_username", "alice")
    assert result is True


def test_whitelist_cross_platform_isolation():
    """gitcode whitelist does not affect github."""
    # gitcode: alice in whitelist -> allow
    result1 = _run_is_user_allowed("true", 1, (1,), "gitcode_username", "alice")
    assert result1 is True
    # github: bob not in whitelist -> deny
    result2 = _run_is_user_allowed("true", 1, None, "github_username", "bob")
    assert result2 is False


def test_whitelist_empty_table_enabled_allows_all():
    """P2-6: switch on but table empty -> allow all (no lockout)."""
    result = _run_is_user_allowed("true", 0, None, "gitcode_username", "anyuser")
    assert result is True


def test_whitelist_invalid_column():
    """SQL injection attempt in column name -> deny."""
    result = _run_is_user_allowed("true", 1, (1,), "gitcode_username; DROP TABLE--", "alice")
    assert result is False


def test_whitelist_explicit_disabled_allows_all():
    """meta=false -> switch explicitly off, allow all even with table data."""
    result = _run_is_user_allowed("false", 1, (1,), "gitcode_username", "bob")
    assert result is True
