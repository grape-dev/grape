"""Chat 任务场景测试：conv_type 标记、build_task_chat_prompt、send_message 分流。"""
import os
import sys
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
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
    assert "## 场景: task" in prompt
    assert "## 任务 Issue" in prompt
    assert "Issue: #7" in prompt
    assert "标题: Issue #7" in prompt
    assert "对话历史" in prompt
    assert "Skills: chat" in prompt


def test_build_task_chat_prompt_no_conv_defaults():
    prompt = build_task_chat_prompt("hi", "owner/repo", [], None)
    assert "## 场景: task" in prompt
    assert "Issue: #0" in prompt
