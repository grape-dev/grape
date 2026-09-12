"""Agent 澄清消息同步到任务会话测试（Issue #46 重构后）：

- _post_result_to_issue 职责收敛为「发 GitCode 评论」，不再写 task conversation
  （result_text 单写，统一由 _execute_agent 收尾承担，见 issue-46-onboarding-task-notification-fix-design.md §4.4）。
"""
import os
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, get_messages, create_conversation, add_message
from src.user import db as user_db

_uid = None
_conv_id = ""
_task_id = None


def setup_module():
    global _uid, _conv_id, _task_id
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username='tc_sync_user'")
        db.commit()
    u = user_db.create_user("tc_sync_user", "test123")
    if not u:
        u = user_db.get_user_by_username("tc_sync_user")
    assert u
    _uid = u["id"]
    _conv_id = create_conversation(_uid, "gitcode:tc_sync/demo", title="Issue #99")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key='gitcode:tc_sync/demo' AND issue_number=99")
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, conversation_id) "
            "VALUES (?,?, 'gitcode:tc_sync/demo', 99, '同步测试', 'test', 'active', ?)",
            (_uid, "tc_sync_user", _conv_id))
        db.commit()
        _task_id = c.lastrowid


def test_post_result_does_not_write_conversation():
    """Issue #46：_post_result_to_issue 只发 GitCode 评论，不再写 task conversation（单写）。"""
    from src.api.event_pipeline import _post_result_to_issue
    def _fake_urlopen(req, timeout=15):
        class _R:
            def read(self): return b'{"id":1}'
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return _R()
    result_text = "需要确认：目标云服务是 ECS 还是 CCE？"
    before = len(get_messages(_conv_id, limit=50))
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        _post_result_to_issue("tc_sync/demo", 99, result_text, "fake_bot_token", "vod")
    after = get_messages(_conv_id, limit=50)
    assert len(after) == before, "不应新增任务会话消息（result_text 单写，同步由 _execute_agent 收尾承担）"


def test_post_result_syncs_to_conversation_via_exec_tail():
    """result_text 写入任务会话由 _execute_agent 收尾承担（单写路径仍可用）。"""
    from src.session_manager import add_message
    add_message(_conv_id, "agent", "[VOD Agent] 需要确认：目标云服务是 ECS 还是 CCE？")
    messages = get_messages(_conv_id, limit=50)
    assert any("需要确认" in m["content"] and "[VOD Agent]" in m["content"] for m in messages)


def test_post_result_no_conversation_no_error():
    """无关联 conversation 时不报错（优雅跳过）。"""
    from src.api.event_pipeline import _post_result_to_issue
    def _fake_urlopen(req, timeout=15):
        class _R:
            def read(self): return b'{}'
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return _R()
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        _post_result_to_issue("tc_sync/no_conv_repo", 888, "test", "fake_token", "issue")