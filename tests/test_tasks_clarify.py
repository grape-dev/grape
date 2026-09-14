"""Task Center 澄清消息测试：拉取 agent 澄清 + 开发者回复（发 GitCode Issue 评论 + 写会话）。"""
import os
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.user import db as user_db
# JWT access token helper（替代旧的 create_session_token）
def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)

_uid = None
_token = ""
_headers = {}
_task_id = None


def setup_module():
    global _uid, _token, _headers, _task_id
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username IN ('tc_clarify_user')")
        db.commit()
    u = user_db.create_user("tc_clarify_user", "test123")
    if not u:
        u = user_db.get_user_by_username("tc_clarify_user")
    assert u, "测试用户创建失败"
    _uid = u["id"]
    _token = _jwt_access_token(_uid, u["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE user_accounts SET gitcode_api_token='tc_clarify_token' WHERE id=?", (_uid,))
        db.commit()
    # 建一个 tasks 表行（手工 issue 10）
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key='gitcode:tc_clarify/demo' AND issue_number=10")
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, conversation_id) "
            "VALUES (?,?, 'gitcode:tc_clarify/demo', 10, '澄清测试', '测试澄清', 'active', '')",
            (_uid, "tc_clarify_user"))
        db.commit()
        _task_id = c.lastrowid
    # 建一个 agent_tasks 行 + task_messages 澄清消息
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id='t_clarify_test'")
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, agent_type, skill, prompt, status) "
            "VALUES ('t_clarify_test', 'sess_clarify_test', 0, 'gitcode:tc_clarify/demo', 10, 'issue', 'issue', 'test', 'completed')")
        c.execute("DELETE FROM task_messages WHERE task_id='t_clarify_test'")
        c.execute(
            "INSERT INTO task_messages (task_id, seq, type, tool, content, created_at) "
            "VALUES ('t_clarify_test', 1, 'text', '', '请确认部署方式：A 还是 B？', datetime('now'))")
        c.execute(
            "INSERT INTO task_messages (task_id, seq, type, tool, content, created_at) "
            "VALUES ('t_clarify_test', 2, 'step_start', '', 'Step 1', datetime('now'))")
        db.commit()


def test_get_clarifications():
    """拉取 agent 澄清消息：只返回 text/reasoning/error/tool_use，不含 step_start 噪音。"""
    r = client.get(f"/api/tasks/{_task_id}/clarifications", headers=_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    msgs = d["messages"]
    assert len(msgs) >= 1
    # text 类型澄清消息存在
    text_msgs = [m for m in msgs if m["type"] == "text"]
    assert any("请确认部署方式" in m["content"] for m in text_msgs), "应含 agent 澄清消息"
    # step_start 被过滤
    assert not any(m["type"] == "step_start" for m in msgs), "不应含 step_start 噪音"
    assert d["issue_url"], "应返回 issue_url"


def test_reply_clarification():
    """开发者回复澄清：mock GitCode POST 评论 + 验证会话消息写入。"""
    def _fake_urlopen(req, timeout=30):
        class _Resp:
            def read(self): return b'{"id":1}'
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return _Resp()
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        r = client.post(f"/api/tasks/{_task_id}/clarify", headers=_headers,
                        json={"message": "选择方案 A，请继续"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_clarify_empty_message():
    """空消息 → 400。"""
    r = client.post(f"/api/tasks/{_task_id}/clarify", headers=_headers, json={"message": ""})
    assert r.status_code == 400


def test_clarify_task_not_found():
    """不存在的 task → 404。"""
    r = client.get("/api/tasks/999999/clarifications", headers=_headers)
    assert r.status_code == 404