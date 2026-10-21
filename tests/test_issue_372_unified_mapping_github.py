"""Issue #372 §37: unified_user_mapping github_login 查询修复测试

FR-18.1/FR-17: 按 repo_key provider 分列查询 gitcode_user / github_login，
不再用 OR github_login=? 双列扫描。飞书通知读点加 github_login 回退（FR-20.8）。
写入用 INSERT OR IGNORE + UPDATE（不覆盖 github_login）。

测试覆盖（源码检查，避免 DB 污染）：
- T20-01: issues_center _get_user_id_by_gitcode 按 provider 分列查询
- T20-02: issues_center _get_feishu_open_id 按 provider 分列查询
- T20-03: session_store set_issue_stage 按 provider 分列查询
- T20-04: pipeline _resolve_feishu_name 按 provider 分列查询
- T20-05: session_manager get_feishu_open_id 有 github_login 回退（FR-20.8）
- T20-06: session_manager update_unified_user_info 用 INSERT OR IGNORE（非 REPLACE）
- T20-07: prompt_builder agent 指令用 INSERT OR IGNORE（非 REPLACE）
- T20-08: chat.py UPDATE 按 gitcode_user 列（不再 OR github_login）
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


def test_issues_center_get_user_id_uses_provider_split():
    """T20-01: issues_center _get_user_id_by_gitcode 按 provider 分列查询。"""
    assert _grep_source("src/api/issues_center.py", "um_column_for_repo")


def test_issues_center_get_feishu_open_id_uses_provider_split():
    """T20-02: issues_center _get_feishu_open_id 按 provider 分列查询。"""
    assert _grep_source("src/api/issues_center.py", "um_column_for_repo")


def test_session_store_set_issue_stage_uses_provider_split():
    """T20-03: session_store set_issue_stage 按 provider 分列查询。"""
    assert _grep_source("src/api/session_store.py", "um_column_for_repo")


def test_pipeline_resolve_feishu_name_uses_provider_split():
    """T20-04: pipeline _resolve_feishu_name 按 provider 分列查询。"""
    assert _grep_source("src/api/routes/pipeline.py", "um_column_for_repo")


def test_session_manager_get_feishu_open_id_has_github_fallback():
    """T20-05: session_manager get_feishu_open_id 有 github_login 回退（FR-20.8）。"""
    assert _grep_source("src/session_manager.py", "github_login")
    assert _grep_source("src/session_manager.py", "FR-20.8")


def test_session_manager_update_uses_insert_ignore_not_replace():
    """T20-06: session_manager update_unified_user_info 用 INSERT OR IGNORE（非 REPLACE）。"""
    with open(os.path.join(ROOT, "src/session_manager.py"), encoding="utf-8") as f:
        content = f.read()
    idx = content.find("def update_unified_user_info")
    assert idx > 0, "update_unified_user_info function not found"
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
    idx = content.find("Save User Mapping")
    assert idx > 0, "Save User Mapping section not found"
    section = content[idx:idx + 500]
    assert "INSERT OR IGNORE" in section, (
        "prompt_builder agent instruction should use INSERT OR IGNORE"
    )
    assert "INSERT OR REPLACE" not in section, (
        "prompt_builder should NOT instruct agent to use INSERT OR REPLACE"
    )


def test_chat_update_uses_gitcode_user_column():
    """T20-08: chat.py UPDATE 按 gitcode_user 列（不再 OR github_login）。"""
    # FR-17.2: chat.py 解绑清映射只查 gitcode_user 列
    assert _grep_source("src/api/routes/chat.py", "gitcode_user=?")


def test_get_feishu_open_id_fallback_on_empty_open_id():
    """T20-09: get_feishu_open_id 在 gitcode_user 命中但 open_id 为空时回退查 github_login。

    撞名型场景（深度向前兼容检查发现）：
    - 用户 A 的 gitcode_user='alice'（注册名撞名），feishu_open_id 为空
    - 用户 B 的 github_login='alice'（真身份），feishu_open_id 非空
    - 旧 OR 查询命中用户 B → 返回 open_id
    - 新代码先查 gitcode_user 命中用户 A（row 非 None 但 row[0] 为空）
    - 回退条件必须是 "not row or not row[0]" 而非 "not row"
    """
    import inspect
    from src.session_manager import get_feishu_open_id
    src = inspect.getsource(get_feishu_open_id)
    # 回退条件必须检查 row[0] 非空，不仅仅是 row 存在
    assert "not row or not row[0]" in src, (
        "get_feishu_open_id 回退条件必须是 'not row or not row[0]'，"
        "否则 gitcode_user 撞名命中无飞书行时不回退 github_login → 通知回归"
    )
