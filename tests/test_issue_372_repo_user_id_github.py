"""Issue #372 §36: _repo_user_id 不查 github_login 导致 GitHub issue user_id 错误测试

_repo_user_id 第 1 步只查 gitcode_user，GitHub actor 查不到 → fallback 到
grape_accounts.username → 命中旧账号。修复：加 OR github_login=?。

测试覆盖（mock-based，避免全量 collection DB 污染）：
- T19-01: GitHub actor 查 github_login 命中正确 user_id
- T19-02: GitCode actor 仍查 gitcode_user（不受影响）
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest.mock import patch, MagicMock


def test_github_actor_matches_github_login():
    """T19-01: GitHub actor 查 github_login 命中正确 user_id。

    修复前：只查 gitcode_user → 查不到 → fallback grape_accounts → 旧账号。
    修复后：OR github_login=? → 命中正确 user_id。
    """
    from src.runtime.engine_select import _repo_user_id

    # Mock DB: unified_user_mapping has user_id=14, gitcode_user="xiaowu", github_login="yangaiwu"
    # GitHub actor="yangaiwu" → should match github_login → return 14
    mock_cursor = MagicMock()
    # Step 1: unified_user_mapping query returns user_id=14
    mock_cursor.fetchone.return_value = (14,)
    mock_db = MagicMock()
    mock_db.cursor.return_value = mock_cursor

    with patch("src.runtime.engine_select._db", return_value=(mock_db, MagicMock())):
        result = _repo_user_id("github:owner/test-repo", issue_creator="yangaiwu")

    assert result == 14, (
        f"GitHub actor 应通过 github_login 命中 user_id=14，实际 {result}"
    )
    # Verify the SQL contains OR github_login
    sql_arg = mock_cursor.execute.call_args_list[0][0][0]
    assert "github_login" in sql_arg, (
        f"SQL 应包含 github_login 字段查询，实际 SQL: {sql_arg}"
    )
    assert "OR" in sql_arg.upper(), f"SQL 应包含 OR 条件，实际: {sql_arg}"


def test_gitcode_actor_still_matches_gitcode_user():
    """T19-02: GitCode actor 仍查 gitcode_user（不受影响）。"""
    from src.runtime.engine_select import _repo_user_id

    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (7,)  # gitcode_user match → user_id=7
    mock_db = MagicMock()
    mock_db.cursor.return_value = mock_cursor

    with patch("src.runtime.engine_select._db", return_value=(mock_db, MagicMock())):
        result = _repo_user_id("gitcode:owner/test-repo", issue_creator="gc_user_123")

    assert result == 7, f"GitCode actor 应通过 gitcode_user 命中 user_id=7，实际 {result}"


def test_sql_query_includes_github_login():
    """T19-03: 验证 _repo_user_id 第 1 步 SQL 包含 github_login 字段（防回归）。"""
    import inspect
    from src.runtime.engine_select import _repo_user_id

    src = inspect.getsource(_repo_user_id)
    # Find the unified_user_mapping query
    assert "github_login" in src, (
        "_repo_user_id 必须在 unified_user_mapping 查询中包含 github_login 字段"
    )
    assert "OR github_login" in src, (
        "_repo_user_id 的 SQL 必须有 OR github_login=? 条件"
    )
