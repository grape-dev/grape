"""Issue #372 §37: unified_user_mapping 全量 github_login 查询修复测试

所有查询 unified_user_mapping 的地方都应支持 OR github_login=?，
所有写入都应用 INSERT OR IGNORE + UPDATE（不覆盖 github_login）。

测试覆盖（源码检查，避免 DB 污染）：
- T20-01: issues_center _get_user_id_by_gitcode SQL 含 github_login
- T20-02: issues_center _get_feishu_open_id SQL 含 github_login
- T20-03: session_store set_issue_stage SQL 含 github_login
- T20-04: pipeline _resolve_feishu_name SQL 含 github_login
- T20-05: session_manager get_feishu_open_id_by_gitcode SQL 含 github_login
- T20-06: session_manager update_unified_user_info 用 INSERT OR IGNORE（非 REPLACE）
- T20-07: prompt_builder agent 指令用 INSERT OR IGNORE（非 REPLACE）
- T20-08: chat.py UPDATE 含 OR github_login
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))


def _grep_source(filepath, pattern):
    """读文件内容，返回是否包含 pattern。"""
    with open(os.path.join(ROOT, filepath), encoding="utf-8") as f:
        return pattern in f.read()


def test_issues_center_get_user_id_has_github_login():
    """T20-01: issues_center _get_user_id_by_gitcode SQL 含 github_login。"""
    assert _grep_source("src/api/issues_center.py", "github_login")
    assert _grep_source("src/api/issues_center.py", "OR um.github_login")


def test_issues_center_get_feishu_open_id_has_github_login():
    """T20-02: issues_center _get_feishu_open_id SQL 含 github_login。"""
    # The function should query OR github_login
    assert _grep_source("src/api/issues_center.py", "OR github_login=?")


def test_session_store_set_issue_stage_has_github_login():
    """T20-03: session_store set_issue_stage SQL 含 github_login。"""
    assert _grep_source("src/api/session_store.py", "m.github_login")


def test_pipeline_resolve_feishu_name_has_github_login():
    """T20-04: pipeline _resolve_feishu_name SQL 含 github_login。"""
    assert _grep_source("src/api/routes/pipeline.py", "OR github_login=?")


def test_session_manager_get_feishu_open_id_has_github_login():
    """T20-05: session_manager get_feishu_open_id_by_gitcode SQL 含 github_login。"""
    assert _grep_source("src/session_manager.py", "OR github_login=?")
    # Also check the stored query in update_unified_user_info
    assert _grep_source("src/session_manager.py", "WHERE gitcode_user=? OR github_login=?")


def test_session_manager_update_uses_insert_ignore_not_replace():
    """T20-06: session_manager update_unified_user_info 用 INSERT OR IGNORE（非 REPLACE）。"""
    # Check specifically: no INSERT OR REPLACE into unified_user_mapping exists
    with open(os.path.join(ROOT, "src/session_manager.py"), encoding="utf-8") as f:
        content = f.read()
    # Find update_unified_user_info function body
    idx = content.find("def update_unified_user_info")
    assert idx > 0, "update_unified_user_info function not found"
    # Get function body up to next def
    next_def = content.find("\ndef ", idx + 1)
    if next_def < 0:
        next_def = idx + 3000
    body = content[idx:next_def]
    assert "INSERT OR IGNORE" in body, (
        "update_unified_user_info should use INSERT OR IGNORE"
    )
    assert "INSERT OR REPLACE INTO unified_user_mapping" not in body, (
        "update_unified_user_info should NOT use INSERT OR REPLACE for unified_user_mapping"
    )


def test_prompt_builder_uses_insert_ignore_not_replace():
    """T20-07: prompt_builder agent 指令用 INSERT OR IGNORE（非 REPLACE）。"""
    with open(os.path.join(ROOT, "src/prompt_builder.py"), encoding="utf-8") as f:
        content = f.read()
    # Find the Save User Mapping section
    idx = content.find("Save User Mapping")
    assert idx > 0, "Save User Mapping section not found"
    section = content[idx:idx + 500]
    assert "INSERT OR IGNORE" in section, (
        "prompt_builder agent instruction should use INSERT OR IGNORE"
    )
    assert "INSERT OR REPLACE" not in section, (
        "prompt_builder should NOT instruct agent to use INSERT OR REPLACE"
    )


def test_chat_update_has_github_login():
    """T20-08: chat.py UPDATE unified_user_mapping 含 OR github_login。"""
    assert _grep_source("src/api/routes/chat.py", "OR github_login=?")
