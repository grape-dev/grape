"""Issue #372 §36: _repo_user_id GitHub actor 匹配测试

FR-18.1: 按 repo_key provider 分列查询 gitcode_user / github_login。
GitHub actor 查 github_login 列命中正确 user_id。

测试覆盖（mock-based，避免全量 collection DB 污染）：
- T19-01: GitHub actor 查 github_login 命中正确 user_id
- T19-02: GitCode actor 仍查 gitcode_user（不受影响）
- T19-03: 验证 _repo_user_id 按 provider 分列查询（防回归）
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest.mock import patch, MagicMock


def test_github_actor_matches_github_login():
    """T19-01: GitHub actor 查 github_login 命中正确 user_id。

    FR-18.1: 按 provider 分列查询，GitHub 仓库查 github_login 列。
    """
    from src.runtime.engine_select import _repo_user_id

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
    # Verify the SQL uses github_login column (provider-based split, not OR)
    sql_arg = mock_cursor.execute.call_args_list[0][0][0]
    assert "github_login" in sql_arg, (
        f"SQL 应包含 github_login 字段查询，实际 SQL: {sql_arg}"
    )


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
    # Verify the SQL uses gitcode_user column
    sql_arg = mock_cursor.execute.call_args_list[0][0][0]
    assert "gitcode_user" in sql_arg, (
        f"SQL 应包含 gitcode_user 字段查询，实际 SQL: {sql_arg}"
    )


def test_sql_query_uses_provider_split():
    """T19-03: 验证 _repo_user_id 按 provider 分列查询（防回归）。

    FR-CQ-1: 重构后用 um_column_for_repo 统一列名选择，不再 inline if/else。
    """
    import inspect
    from src.runtime.engine_select import _repo_user_id

    src = inspect.getsource(_repo_user_id)
    assert "um_column_for_repo" in src, (
        "_repo_user_id 必须用 um_column_for_repo 做分列查询"
    )
    assert "parse_repo_key" in src, (
        "_repo_user_id 必须用 parse_repo_key 获取 provider（回退链需要）"
    )
