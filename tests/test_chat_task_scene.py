"""Chat 任务场景测试：conv_type 标记、build_task_chat_prompt、send_message 分流。"""
import os
import sys
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, create_conversation, get_conversation, update_conversation
from src.prompt_builder import build_task_chat_prompt


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_conversations")
        c.execute("DELETE FROM chat_messages")
        db.commit()


def test_create_task_conversation_marks_type():
    _clean()
    cid = create_conversation(1, "owner/repo", title="Issue #5", conv_type="task", issue_number=5)
    conv = get_conversation(cid)
    assert conv["conv_type"] == "task"
    assert conv["issue_number"] == 5
    _clean()


def test_default_conversation_is_general():
    _clean()
    cid = create_conversation(1, "owner/repo", title="新对话")
    conv = get_conversation(cid)
    assert conv["conv_type"] == "general"
    assert conv["issue_number"] == 0
    _clean()


def test_update_conversation_type():
    _clean()
    cid = create_conversation(1, "owner/repo", title="Issue #5")
    update_conversation(cid, conv_type="task", issue_number=5)
    conv = get_conversation(cid)
    assert conv["conv_type"] == "task"
    assert conv["issue_number"] == 5
    _clean()


def test_build_task_chat_prompt_contains_scene_and_context():
    conv = {"conv_type": "task", "issue_number": 7, "title": "Issue #7", "repo_key": "owner/repo"}
    history = [{"role": "user", "content": "📋 任务分配 · Issue #7"}, {"role": "agent", "content": "收到"}]
    prompt = build_task_chat_prompt("我需要澄清一下", "owner/repo", history, conv)
    assert "## Scene: task" in prompt
    assert "## Task Context" in prompt
    assert "Issue: #7" in prompt
    assert "Title: Issue #7" in prompt
    assert "## Chat History" in prompt
    assert "## Current Skill: chat" in prompt


def test_build_task_chat_prompt_injects_task_boundary():
    """任务会话 prompt 职责并入 Role「当前职责」：仅澄清+回帖，禁止部署/写代码/git（chat 越权部署修复）。

    2026-08-29 重构：任务边界从独立「### 任务边界」段合并进 Role「当前职责」。
    """
    conv = {"conv_type": "task", "issue_number": 7, "title": "Issue #7", "repo_key": "owner/repo"}
    history = [{"role": "user", "content": "📋 任务分配 · Issue #7"}]
    prompt = build_task_chat_prompt("方式2", "owner/repo", history, conv)
    assert "## Current Role (task conversation)" in prompt
    assert "requirement-clarification" in prompt
    assert "Do NOT implement / deploy / write code" in prompt
    assert "/ run git commands" in prompt
    assert "pipeline" in prompt and "coding" in prompt
    # 职责强位置：Role 位于提示开头，场景段之后才是任务数据（职责必经场景前）
    assert prompt.index("## Current Role (task conversation)") < prompt.index("## Scene: task")


def test_build_task_chat_prompt_no_conv_defaults():
    prompt = build_task_chat_prompt("hi", "owner/repo", [], None)
    assert "## Scene: task" in prompt
    assert "Issue: #0" in prompt


def _clean_tasks():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue")
        db.commit()


def _insert_task(repo_key, issue_number, description, conv_id="", user_id=1):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, conversation_id, status) VALUES (?,?,?,?,?,?,?, 'active')",
            (user_id, "alice", repo_key, issue_number, f"Issue #{issue_number}", description, conv_id))
        db.commit()


def test_build_task_chat_prompt_injects_task_description():
    """任务会话 prompt 显式含 ## 任务描述（来自 tasks 表）"""
    _clean()
    _clean_tasks()
    _insert_task("gitcode:owner/repo", 7, "处理登录超时")
    conv = {"conv_type": "task", "issue_number": 7, "title": "Issue #7",
            "repo_key": "gitcode:owner/repo", "id": ""}
    prompt = build_task_chat_prompt("确认一下", "gitcode:owner/repo", [], conv)
    assert "### Task Description" in prompt
    assert "处理登录超时" in prompt
    _clean_tasks()
    _clean()


def test_build_task_chat_prompt_no_description_ok():
    """tasks 表无对应记录时 prompt 正常（跳过 ## 任务描述）"""
    _clean()
    _clean_tasks()
    conv = {"conv_type": "task", "issue_number": 99, "title": "Issue #99",
            "repo_key": "gitcode:owner/repo", "id": ""}
    prompt = build_task_chat_prompt("hi", "gitcode:owner/repo", [], conv)
    assert "### Task Description" not in prompt
    assert "## Scene: task" in prompt
    assert "Issue: #99" in prompt
    _clean()


def test_build_task_chat_prompt_by_conversation_id():
    """conv.id 非空时按 conversation_id 反查（优先于 repo+issue）"""
    _clean()
    _clean_tasks()
    cid = create_conversation(1, "owner/repo", title="Issue #8", conv_type="task", issue_number=8)
    _insert_task("gitcode:owner/repo", 8, "通过 conversation_id 找到我", conv_id=cid)
    conv = get_conversation(cid)
    prompt = build_task_chat_prompt("继续", "owner/repo", [], conv)
    assert "通过 conversation_id 找到我" in prompt
    _clean_tasks()
    _clean()


def test_build_task_chat_prompt_description_survives_long_history():
    """任务描述结构化注入，不依赖 history；>10 条历史后仍在"""
    _clean()
    _clean_tasks()
    _insert_task("gitcode:owner/repo", 10, "长对话后的任务描述")
    long_history = [{"role": "user" if i % 2 == 0 else "agent", "content": f"msg{i}"} for i in range(15)]
    conv = {"conv_type": "task", "issue_number": 10, "title": "Issue #10",
            "repo_key": "gitcode:owner/repo", "id": ""}
    prompt = build_task_chat_prompt("hi", "gitcode:owner/repo", long_history, conv)
    assert "长对话后的任务描述" in prompt
    _clean_tasks()
    _clean()
