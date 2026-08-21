"""Agent 澄清消息同步到任务会话测试：_post_result_to_issue 发评论后 conversation 可见。"""
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
        c.execute("DELETE FROM tasks WHERE repo_key='gitcode:tc_sync/demo' AND issue_number=99")
        c.execute(
            "INSERT INTO tasks (user_id, assignee, repo_key, issue_number, title, task_description, status, conversation_id) "
            "VALUES (?,?, 'gitcode:tc_sync/demo', 99, '同步测试', 'test', 'active', ?)",
            (_uid, "tc_sync_user", _conv_id))
        db.commit()
        _task_id = c.lastrowid


def test_post_result_syncs_to_conversation():
    """_post_result_to_issue 发 Issue 评论后，task conversation 出现 agent 消息。"""
    from src.api.event_pipeline import _post_result_to_issue
    # mock GitCode API POST（不真实发评论）
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
    assert len(after) > before, "应新增一条 agent 消息"
    last = after[-1]
    assert last["role"] == "agent"
    assert "需要确认" in last["content"]
    assert "[VOD Agent]" in last["content"]


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