"""Chat 任务场景测试：conv_type 标记、build_issue_chat_prompt、send_message 分流。"""
import os
import sys
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, create_conversation, get_conversation, update_conversation
from src.prompt_builder import build_issue_chat_prompt


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


def test_build_issue_chat_prompt_contains_scene_and_context():
    conv = {"conv_type": "task", "issue_number": 7, "title": "Issue #7", "repo_key": "owner/repo"}
    prompt = build_issue_chat_prompt("我需要澄清一下", "owner/repo", [], conv)
    assert "## Scene: issue" in prompt
    assert "## Issue Context" in prompt
    assert "Issue: #7" in prompt
    assert "Title: Issue #7" in prompt
    assert "## Current Skill: chat" in prompt
    # 收尾符（A1：与 general 对齐）
    assert prompt.endswith("\nAssistant:")


def test_build_issue_chat_prompt_role_boundary():
    """issue 会话 prompt Role 指明：围绕 Issue 讨论（澄清/咨询/评审），禁止实现/部署/写代码。"""
    conv = {"conv_type": "task", "issue_number": 7, "title": "Issue #7", "repo_key": "owner/repo"}
    prompt = build_issue_chat_prompt("方式2", "owner/repo", [], conv)
    assert "## Current Role (issue conversation)" in prompt
    # 讨论能力：澄清 + 咨询 + 评审
    assert "Discus" in prompt and "clarif" in prompt
    assert "review proposed approaches" in prompt
    # 红线保留
    assert "Do NOT implement / deploy / write code" in prompt
    assert "/ run git commands" in prompt
    assert "pipeline" in prompt and "coding" in prompt
    # Role 位于提示开头（职责必经场景前）
    assert prompt.index("## Current Role (issue conversation)") < prompt.index("## Scene: issue")


def test_build_issue_chat_prompt_no_conv_defaults():
    prompt = build_issue_chat_prompt("hi", "owner/repo", [], None)
    assert "## Scene: issue" in prompt
    assert "Issue: #0" in prompt


def test_build_issue_chat_prompt_no_task_description():
    """决策 R2：不再注入「## Task Description」，改由 Issue 上下文承担。"""
    conv = {"conv_type": "task", "issue_number": 7, "title": "Issue #7", "repo_key": "gitcode:owner/repo"}
    prompt = build_issue_chat_prompt("确认一下", "gitcode:owner/repo", [], conv)
    assert "### Task Description" not in prompt


def test_build_issue_chat_prompt_injects_language_rule():
    """回复语言跟随用户（A2/D7）。"""
    conv = {"conv_type": "task", "issue_number": 7, "title": "Issue #7", "repo_key": "owner/repo"}
    prompt = build_issue_chat_prompt("hi", "owner/repo", [], conv)
    assert "same language the user used" in prompt


def test_build_issue_chat_prompt_history_injected():
    conv = {"conv_type": "task", "issue_number": 7, "title": "Issue #7", "repo_key": "owner/repo"}
    history = [{"role": "user", "content": "📋 任务分配 · Issue #7"}, {"role": "agent", "content": "收到"}]
    prompt = build_issue_chat_prompt("继续", "owner/repo", history, conv)
    assert "## Chat History" in prompt
    assert "## Current User Message" in prompt