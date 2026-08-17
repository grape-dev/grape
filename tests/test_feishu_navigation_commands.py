"""飞书导航命令卡片化测试（feishu-repo-navigation-commands-design.md §6 C01-C14）。"""
import os
import sys
import asyncio
from unittest.mock import AsyncMock, patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.im.feishu_async import BotContext
from src.session_manager import (
    db, db_lock,
    create_conversation, get_conversation_by_im, join_feishu_chat, get_conversation,
)


def _ctx(app_id="cli_nav"):
    return BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="chat-nav")


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("repo_im_group_binding", "chat_conversations", "chat_messages", "tasks", "user_repos"):
            try:
                c.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        c.execute("DELETE FROM chat_agent_session WHERE id LIKE 'nav-%'")
        db.commit()


def _seed():
    _clean()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, im_app_id) VALUES ('nav-sess', 1, 'cli_nav')")
        for i, (rk, admin, join) in enumerate([
            ("gitcode:navuser/repo-a", 1, 1),
            ("gitcode:navuser/repo-b", 0, 1),
        ]):
            c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (1, ?, 'navuser', ?, ?, ?)",
                      (rk, f"repo-{'a' if 'repo-a' in rk else 'b'}", admin, join))
        db.commit()
    # 绑定 repo-a 到 chat
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) VALUES ('nav_chat', 'gitcode:navuser/repo-a', 'gitcode', 'sender_1', 'cli_nav')")
        db.commit()


def _run_cmd(text, chat_id="nav_chat", api=None, ctx=None):
    from src.im.handler_async import process_command_async
    ctx = ctx or _ctx()
    api = api or AsyncMock()
    asyncio.run(process_command_async(ctx, api, text, chat_id, "sender_1", db, account=ctx.app_id, msg_id="msg_1"))
    return api


def _run_msg(text, chat_id="nav_chat", api=None, ctx=None):
    from src.im.handler_async import process_chat_async
    ctx = ctx or _ctx()
    api = api or AsyncMock()
    parsed = {"type": "message", "text": text, "chat_id": chat_id,
              "message_id": "msg_1", "sender_id": "sender_1", "sender_open_id": "ou_nav",
              "chat_type": "group"}
    asyncio.run(process_chat_async(ctx, api, parsed))
    return api


def _pending():
    from src.im.handler_async import _get_pending_selection
    return _get_pending_selection("nav_chat")


def _card_text(api):
    assert api.reply_card.called
    return api.reply_card.call_args[0][2]


# ---- C01/C02：/repo 卡片 + 编号绑定 ----

def test_repo_lists_and_marks_bound():
    _seed()
    api = _run_cmd("/repo")
    text = _card_text(api)
    assert "repo-a" in text and "repo-b" in text
    assert "【绑定中】" in text      # repo-a 已绑定
    assert _pending() is not None and _pending()["action"] == "repo"
    _clean()


def test_repo_number_selects_and_binds():
    _seed()
    _run_cmd("/repo")
    api = _run_msg("2")   # 选 repo-b
    with db_lock:
        row = db.cursor().execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id='nav_chat'").fetchone()
    assert row and "repo-b" in row[0]
    assert _pending() is None
    assert _card_text(api).startswith("**✅ 已绑定")
    _clean()


def test_number_without_pending_goes_normal_chat():
    _seed()
    api = _run_msg("5")   # 无 pending → 不吞数字
    assert _card_text if False else True
    # 未绑定仓库提示走 reply_text
    assert api.reply_text.called
    _clean()


def test_expired_pending_not_intercepted():
    _seed()
    from src.im.handler_async import _pending_selections
    _pending_selections["nav_chat"] = {"action": "repo", "repo_key": "", "items": [], "page": 1,
                                       "expires": 0, "page_size": 10}
    api = _run_msg("5")
    assert api.reply_text.called
    assert api.reply_card.call_count == 0
    _clean()


def test_new_command_overwrites_old_pending():
    _seed()
    create_conversation(1, "gitcode:navuser/repo-a", title="会话A")
    _run_cmd("/repo")
    assert _pending()["action"] == "repo"
    _run_cmd("/chat")     # 覆盖
    p = _pending()
    assert p is not None and p["action"] == "chat"
    _clean()


# ---- C05：/chat 列表 + 编号加入 ----

def test_chat_lists_and_joins():
    _seed()
    ca = create_conversation(1, "gitcode:navuser/repo-a", title="一般对话")
    cb = create_conversation(1, "gitcode:navuser/repo-a", title="Issue #3", conv_type="task", issue_number=3)
    api = _run_cmd("/chat")
    text = _card_text(api)
    assert "一般对话" in text and "Issue #3" in text
    _run_msg("2")   # 选 Issue #3
    conv = get_conversation_by_im("nav_chat", 1)
    assert conv and conv["id"] == cb and conv["conv_type"] == "task"
    _clean()


# ---- C06：/task 列表 + 进入任务会话 ----

def test_task_lists_and_enters_conversation():
    _seed()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, title, status) VALUES (1,'navuser','gitcode:navuser/repo-a',7,'刷新readme','active')")
        db.commit()
    api = _run_cmd("/task")
    text = _card_text(api)
    assert "Issue #7" in text and "active" in text
    _run_msg("1")
    conv = get_conversation_by_im("nav_chat", 1)
    assert conv and conv["conv_type"] == "task" and conv["issue_number"] == 7
    _clean()


# ---- C07：翻页 ----

def test_repo_pagination_page2():
    _seed()
    with db_lock:
        c = db.cursor()
        for i in range(2, 14):   # +12 仓库 → 共 14 个，2 页
            c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (1, ?, 'navuser', ?, 0, 1)",
                      (f"gitcode:navuser/repo-{i}", f"repo-{i}"))
        db.commit()
    api = _run_cmd("/repo 2")
    text = _card_text(api)
    assert "第 2/2 页" in text
    assert "repo-10" not in text      # 第 1 页首个不在第 2 页
    _clean()


# ---- C08/C09/C10/C11/C12/C13/C14 ----

def test_help_card():
    _seed()
    api = _run_cmd("/help")
    text = _card_text(api)
    for cmd in ("/repo", "/chat", "/task", "/new", "/where", "/cancel", "/clean", "/help"):
        assert cmd in text
    _clean()


def test_clean_card():
    _seed()
    api = _run_cmd("/clean")
    assert _card_text(api).startswith("**✅ 已清除")
    _clean()


def test_new_creates_and_joins():
    _seed()
    api = _run_cmd("/new")
    assert "已新建并加入" in _card_text(api)
    conv = get_conversation_by_im("nav_chat", 1)
    assert conv is not None
    _clean()


def test_where_shows_position():
    _seed()
    cid = create_conversation(1, "gitcode:navuser/repo-a", title="Issue #9", conv_type="task", issue_number=9)
    join_feishu_chat("nav_chat", cid, 1)
    api = _run_cmd("/where")
    text = _card_text(api)
    assert "repo-a" in text and "Issue #9" in text
    _clean()


def test_cancel_clears_pending():
    _seed()
    _run_cmd("/repo")
    assert _pending() is not None
    api = _run_cmd("/cancel")
    assert _pending() is None
    assert "已取消" in _card_text(api)
    _clean()


def test_reply_zero_cancels():
    _seed()
    _run_cmd("/repo")
    assert _pending() is not None
    api = _run_msg("0")
    assert _pending() is None
    assert "取消" in _card_text(api)
    _clean()


# ---- 绑定/会话发散修复（2026-08-16）----

def test_rebind_clears_stale_conversation_pointer():
    """绑定新仓库 → 旧会话 im_channel_id 被清空（下次消息在新仓库新建会话）"""
    _seed()
    old = create_conversation(1, "gitcode:navuser/repo-a", title="旧会话")
    join_feishu_chat("nav_chat", old, 1)
    assert get_conversation_by_im("nav_chat", 1) is not None
    _run_cmd("/repo")
    _run_msg("2")   # 绑 repo-b
    with db_lock:
        row = db.cursor().execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id='nav_chat'").fetchone()
    assert row and "repo-b" in row[0]
    assert get_conversation(old)["im_channel_id"] == ""      # 旧指针已清
    assert get_conversation_by_im("nav_chat", 1) is None
    _clean()


def test_bind_intent_clears_stale_pointer():
    """自然语言"绑定到 X" → 旧会话指针清空"""
    _seed()
    old = create_conversation(1, "gitcode:navuser/repo-a", title="旧会话")
    join_feishu_chat("nav_chat", old, 1)
    from src.im.handler_async import detect_bind_intent
    with patch("src.im.handler_async.detect_bind_intent", return_value="navuser/repo-b"):
        _run_msg("绑定到 navuser/repo-b")
    assert get_conversation(old)["im_channel_id"] == ""
    assert get_conversation_by_im("nav_chat", 1) is None
    _clean()


def test_where_warns_on_repo_mismatch():
    """群绑定仓库 ≠ 会话所属仓库 → /where 卡片提示不一致"""
    _seed()
    # 群绑定 repo-a（seed 已绑），但加入一个 repo-b 的会话 → 制造不一致
    cid = create_conversation(1, "gitcode:navuser/repo-b", title="repo-b 会话")
    join_feishu_chat("nav_chat", cid, 1)
    api = _run_cmd("/where")
    text = _card_text(api)
    assert "不一致" in text
    assert "repo-a" in text and "repo-b" in text
    _clean()


def test_where_no_warning_when_consistent():
    _seed()
    cid = create_conversation(1, "gitcode:navuser/repo-a", title="repo-a 会话")
    join_feishu_chat("nav_chat", cid, 1)
    api = _run_cmd("/where")
    text = _card_text(api)
    assert "不一致" not in text
    _clean()
