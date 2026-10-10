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
        c.execute("DELETE FROM chat_conversations WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR id LIKE 'test_%' OR title LIKE 'test_%'")
        c.execute("DELETE FROM chat_messages WHERE conversation_id IN (SELECT id FROM chat_conversations WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%')")
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


def _clean_tasks():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
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