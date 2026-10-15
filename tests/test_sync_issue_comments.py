"""_sync_issue_comments_to_conversation 测试：验证 agent 在 Issue 上发的评论能同步到任务会话。"""
import os, json
from unittest.mock import patch, MagicMock

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, get_messages, create_conversation, add_message
from src.user import db as user_db
from src.auth import grape_account_db

_uid = None
_conv_id = ""
_task_id = None


def setup_module():
    global _uid, _conv_id, _task_id
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='tc_sync_issue'")
        db.commit()
    u = grape_account_db.create_grape_account("tc_sync_issue", email=None, password="test123")
    if not u:
        u = grape_account_db.get_grape_account_by_username("tc_sync_issue")
    assert u
    _uid = u["id"]
    _conv_id = create_conversation(_uid, "gitcode:tc_sync_issue/demo", title="Issue #77")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key='gitcode:tc_sync_issue/demo' AND issue_number=77")
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, conversation_id) "
            "VALUES (?,?, 'gitcode:tc_sync_issue/demo', 77, '评论同步测试', 'test', 'active', ?)",
            (_uid, "tc_sync_issue", _conv_id))
        db.commit()
        _task_id = c.lastrowid


def _fake_gitcode_comments():
    """模拟 GitCode API 返回的 Issue 评论列表"""
    return [
        {
            "id": 9001,
            "body": "[VOD Agent] 感谢提交需求。关于部署到华为云，需要确认以下几点：\n\n1. 目标云服务\n2. 部署方式\n3. 额外需求",
            "user": {"login": "grace_bot"},
            "created_at": "2026-08-21T09:27:07+08:00",
        },
        {
            "id": 9002,
            "body": "[issue agent] ✅ 已完成 Issue 分类与指派。\n\n分析结论：这是明确的部署请求。",
            "user": {"login": "grace_bot"},
            "created_at": "2026-08-21T09:28:00+08:00",
        },
        {
            "id": 9003,
            "body": "这是开发者的回复，不是 agent 评论",
            "user": {"login": "songspace"},
            "created_at": "2026-08-21T10:00:00+08:00",
        },
        {
            "id": 9004,
            "body": "> 部署这个博客hello-blog到华为云\n\n[VOD Agent] 您好，此 Issue 与 #8 内容重复。建议关闭此 Issue。",
            "user": {"login": "grace_bot"},
            "created_at": "2026-08-21T10:01:00+08:00",
        },
    ]


def _make_fake_resp(comments):
    fake_resp = MagicMock()
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.read = MagicMock(return_value=json.dumps(comments).encode())
    return fake_resp


def test_sync_issue_comments_writes_agent_comments():
    """_sync_issue_comments_to_conversation 将 Issue 上 agent 评论同步到会话"""
    from src.api.event_pipeline import _sync_issue_comments_to_conversation

    before = len(get_messages(_conv_id, limit=100))
    with patch('urllib.request.urlopen', return_value=_make_fake_resp(_fake_gitcode_comments())):
        _sync_issue_comments_to_conversation(
            "gitcode:tc_sync_issue/demo", 77, "fake_bot_token", repository="tc_sync_issue/demo"
        )
    after = get_messages(_conv_id, limit=100)
    # 应新增 3 条 agent 消息（9003 是开发者评论不同步；9001/9002/9004 含 [XXX Agent] 标记）
    assert len(after) == before + 3, f"应新增 3 条消息，实际新增 {len(after) - before}"
    # 验证内容（[issue agent] 小写前缀也匹配；引用块后的 [VOD Agent] 也匹配）
    all_content = " ".join(m["content"] for m in after)
    assert "目标云服务" in all_content
    assert "已完成 Issue 分类" in all_content
    assert "内容重复" in all_content


def test_sync_issue_comments_dedup():
    """重复调用不产生重复消息（去重）"""
    from src.api.event_pipeline import _sync_issue_comments_to_conversation

    before = len(get_messages(_conv_id, limit=100))
    with patch('urllib.request.urlopen', return_value=_make_fake_resp(_fake_gitcode_comments())):
        _sync_issue_comments_to_conversation(
            "gitcode:tc_sync_issue/demo", 77, "fake_bot_token", repository="tc_sync_issue/demo"
        )
    after = get_messages(_conv_id, limit=100)
    assert len(after) == before, f"去重后不应新增消息，实际新增 {len(after) - before}"


def test_sync_issue_comments_case_insensitive():
    """agent 评论前缀大小写不敏感匹配（[issue agent] 小写也能匹配）"""
    from src.api.event_pipeline import _sync_issue_comments_to_conversation

    # 新 Issue 号避免和前面测试数据冲突
    conv_id2 = create_conversation(_uid, "gitcode:tc_sync_issue/demo", title="Issue #88")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key='gitcode:tc_sync_issue/demo' AND issue_number=88")
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, conversation_id) "
            "VALUES (?,?, 'gitcode:tc_sync_issue/demo', 88, '大小写测试', 'test', 'active', ?)",
            (_uid, "tc_sync_issue", conv_id2))
        db.commit()

    lowercase_comments = [
        {
            "id": 9101,
            "body": "[issue agent] 小写前缀评论测试",
            "user": {"login": "grace_bot"},
            "created_at": "2026-08-21T11:00:00+08:00",
        },
        {
            "id": 9102,
            "body": "[VOD Agent] 大写前缀评论测试",
            "user": {"login": "grace_bot"},
            "created_at": "2026-08-21T11:01:00+08:00",
        },
    ]

    before = len(get_messages(conv_id2, limit=100))
    with patch('urllib.request.urlopen', return_value=_make_fake_resp(lowercase_comments)):
        _sync_issue_comments_to_conversation(
            "gitcode:tc_sync_issue/demo", 88, "fake_bot_token", repository="tc_sync_issue/demo"
        )
    after = get_messages(conv_id2, limit=100)
    assert len(after) == before + 2, f"大小写不敏感应匹配 2 条，实际新增 {len(after) - before}"
    assert any("小写前缀" in m["content"] for m in after)
    assert any("大写前缀" in m["content"] for m in after)


def test_sync_issue_comments_no_conversation():
    """无关联 conversation 时不报错"""
    from src.api.event_pipeline import _sync_issue_comments_to_conversation

    with patch('urllib.request.urlopen', return_value=_make_fake_resp([])):
        _sync_issue_comments_to_conversation(
            "gitcode:no_conv/repo", 999, "fake_token", repository="no_conv/repo"
        )
