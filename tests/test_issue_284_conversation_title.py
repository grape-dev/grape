"""Issue #284: chat agent 会话标题优化测试。

覆盖：
1. 新建会话标题为「Issue #N · {需求标题}」
2. 同一 Issue 重复 ensure 不产生重复会话
3. 存量「Issue #N」会话复用时自动升级为新格式
4. title 缺失时回退「Issue #N」
5. 超长标题截断保护
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest.mock import patch

from src.api import issues_center as ic
from src.session_manager import db, db_lock

REPO = "gitcode:owner/repo284"
USER_ID = 5001


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_conversations WHERE user_id=?", (USER_ID,))
        c.execute("DELETE FROM chat_messages WHERE conversation_id IN "
                  "(SELECT id FROM chat_conversations WHERE user_id=?)", (USER_ID,))
        c.execute("DELETE FROM chat_conversations WHERE user_id=?", (USER_ID,))
        db.commit()


def _cleanup_all():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_messages")
        c.execute("DELETE FROM chat_conversations")
        db.commit()


def setup_function():
    _cleanup_all()


def teardown_function():
    _cleanup_all()


def _get_conv_title(conv_id):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT title FROM chat_conversations WHERE id=?", (conv_id,))
        row = c.fetchone()
    return row[0] if row else None


def _get_conv_count(user_id, repo_key, issue_number):
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT COUNT(*) FROM chat_conversations WHERE user_id=? AND repo_key=? "
            "AND conv_type='task' AND issue_number=?",
            (user_id, repo_key, issue_number))
        return c.fetchone()[0]


def test_new_conversation_title_includes_issue_title():
    """新建会话标题为「Issue #N · {需求标题}」"""
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=USER_ID), \
         patch.object(ic, "_get_issue_title", return_value="chat agent里面的会话标题优化"):
        conv_id = ic._ensure_task_conversation(USER_ID, REPO, 284, title="chat agent里面的会话标题优化")
    assert conv_id, "应返回有效的 conversation_id"
    title = _get_conv_title(conv_id)
    assert title == "Issue #284 · chat agent里面的会话标题优化", f"标题应为「Issue #284 · ...」, got: {title}"


def test_reuse_no_duplicate_conversation():
    """同一 Issue 重复 ensure 不产生重复会话"""
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=USER_ID), \
         patch.object(ic, "_get_issue_title", return_value="测试需求"):
        conv1 = ic._ensure_task_conversation(USER_ID, REPO, 100, title="测试需求")
        conv2 = ic._ensure_task_conversation(USER_ID, REPO, 100, title="测试需求")
    assert conv1 == conv2, "同一 issue 重复 ensure 应复用同一会话"
    assert _get_conv_count(USER_ID, REPO, 100) == 1, "不应产生重复会话"


def test_old_title_upgrade_on_reuse():
    """存量「Issue #N」会话复用时自动升级为新格式"""
    from src.session_manager import create_conversation
    old_conv_id = create_conversation(USER_ID, REPO, title="Issue #200",
                                       conv_type="task", issue_number=200)
    assert _get_conv_title(old_conv_id) == "Issue #200"
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=USER_ID), \
         patch.object(ic, "_get_issue_title", return_value="升级后的标题"):
        new_conv_id = ic._ensure_task_conversation(USER_ID, REPO, 200, title="升级后的标题")
    assert new_conv_id == old_conv_id, "应复用存量会话，不是新建"
    title = _get_conv_title(new_conv_id)
    assert title == "Issue #200 · 升级后的标题", f"旧标题应升级为新格式, got: {title}"


def test_empty_title_fallback():
    """title 为空时回退「Issue #N」"""
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=USER_ID), \
         patch.object(ic, "_get_issue_title", return_value=""):
        conv_id = ic._ensure_task_conversation(USER_ID, REPO, 300, title="")
    assert conv_id, "空标题也应创建会话"
    title = _get_conv_title(conv_id)
    assert title == "Issue #300", f"空标题应回退为 Issue #N, got: {title}"


def test_user_custom_title_not_overwritten():
    """用户自定义标题不被覆盖（仅旧格式 Issue #N 才升级）"""
    from src.session_manager import create_conversation
    custom_conv_id = create_conversation(USER_ID, REPO, title="我的自定义标题",
                                          conv_type="task", issue_number=400)
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=USER_ID), \
         patch.object(ic, "_get_issue_title", return_value="需求标题"):
        reused_conv_id = ic._ensure_task_conversation(USER_ID, REPO, 400, title="需求标题")
    assert reused_conv_id == custom_conv_id, "应复用存量会话"
    title = _get_conv_title(custom_conv_id)
    assert title == "我的自定义标题", f"用户自定义标题不应被覆盖, got: {title}"


def test_long_title_truncated():
    """超长标题截断保护"""
    long_title = "这是一个非常非常非常长的需求标题" * 10
    with patch.object(ic, "_get_user_id_by_gitcode", return_value=USER_ID), \
         patch.object(ic, "_get_issue_title", return_value=long_title):
        conv_id = ic._ensure_task_conversation(USER_ID, REPO, 500, title=long_title)
    assert conv_id
    title = _get_conv_title(conv_id)
    assert title.startswith("Issue #500 · "), f"标题前缀应正确, got: {title}"
    title_part = title[len("Issue #500 · "):]
    assert len(title_part) <= 80, f"标题部分应截断至 80 字符, got len={len(title_part)}"


def test_build_task_conversation_title_unit():
    """_build_task_conversation_title 单元测试"""
    assert ic._build_task_conversation_title(1, "hello") == "Issue #1 · hello"
    assert ic._build_task_conversation_title(1, "") == "Issue #1"
    assert ic._build_task_conversation_title(1, None) == "Issue #1"
    assert ic._build_task_conversation_title(1, "  ") == "Issue #1"
    long_t = "x" * 100
    result = ic._build_task_conversation_title(1, long_t)
    assert result == f"Issue #1 · {'x' * 80}"
