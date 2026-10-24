"""Chat Issue 会话测试：conv_type 标记、build_issue_chat_prompt、send_message 分流。"""
import os

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
    history = [{"role": "user", "content": "📋 任务分配 · Issue #7"}, {"role": "agent", "content": "收到"}]
    prompt = build_issue_chat_prompt("我需要澄清一下", "owner/repo", history, conv)
    assert "## Scene: issue" in prompt
    assert "## Issue Context" in prompt
    assert "Issue: #7" in prompt
    assert "Title: Issue #7" in prompt
    assert "## Chat History" in prompt
    assert "## Current Skill: chat" in prompt


def test_build_issue_chat_prompt_injects_role_boundary():
    """Issue 会话职责：讨论/澄清/回帖，禁止实现、部署、写代码、改仓库状态。

    决策 R1/R2：角色从「任务澄清」放宽为「围绕该 Issue 的讨论」，
    但实现类动作仍由流水线（coding/sre）执行。
    """
    conv = {"conv_type": "task", "issue_number": 7, "title": "Issue #7", "repo_key": "owner/repo"}
    history = [{"role": "user", "content": "📋 任务分配 · Issue #7"}]
    prompt = build_issue_chat_prompt("方式2", "owner/repo", history, conv)
    assert "## Current Role (issue conversation)" in prompt
    assert "Do NOT implement / deploy / write code / run git commands" in prompt
    assert "pipeline" in prompt and "coding" in prompt
    # 职责强位置：Role 位于提示开头，场景段之后才是 Issue 数据（职责必经场景前）
    assert prompt.index("## Current Role (issue conversation)") < prompt.index("## Scene: issue")


def test_build_issue_chat_prompt_no_conv_defaults():
    prompt = build_issue_chat_prompt("hi", "owner/repo", [], None)
    assert "## Scene: issue" in prompt
    assert "Issue: #0" in prompt
