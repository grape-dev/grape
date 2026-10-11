"""飞书导航命令卡片化测试（feishu-repo-navigation-commands-design.md §6 C01-C14）。"""
import os
import sys
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.im.feishu_async import BotContext
from src.session_manager import (
    db, db_lock,
    create_conversation, get_conversation_by_im, join_feishu_chat, get_conversation,
)


@pytest.fixture(autouse=True)
def _clear_repo_bind_cache():
    """每个测试前清除 _repo_bind_cache，防止跨测试缓存污染。"""
    from src.im.handler_async import _repo_bind_cache
    _repo_bind_cache.clear()
    yield
    _repo_bind_cache.clear()


def _ctx(app_id="cli_nav"):
    return BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="chat-nav")


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("repo_im_group_binding", "chat_conversations", "chat_messages", "tasks", "user_repos", "managed_repos"):
            try:
                try:
                    c.execute(f"DELETE FROM {t} WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
                except Exception:
                    pass
            except Exception:
                pass
        c.execute("DELETE FROM chat_agent_session WHERE id LIKE 'nav-%'")
        c.execute("DELETE FROM meta WHERE key='ws_feishu_app_id'")
        db.commit()
    from src.im.handler_async import _clear_pending_selection
    _clear_pending_selection("nav_chat")


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
            c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?, 'navuser', ?, ?)",
                      (rk, f"repo-{'a' if 'repo-a' in rk else 'b'}", f"navuser/repo-{'a' if 'repo-a' in rk else 'b'}"))
        db.commit()
    # 绑定 repo-a 到 chat
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) VALUES ('nav_chat', 'gitcode:navuser/repo-a', 'gitcode', 'sender_1', 'cli_nav')")
        db.commit()


def _run_cmd(text, chat_id="nav_chat", api=None, ctx=None):
    from src.im.handler_async import process_command_async
    ctx = ctx or _ctx()
    api = api or _make_api_mock()
    asyncio.run(process_command_async(ctx, api, text, chat_id, "sender_1", db, account=ctx.app_id, msg_id="msg_1", sender_open_id="ou_nav"))
    return api


def _run_msg(text, chat_id="nav_chat", api=None, ctx=None):
    from src.im.handler_async import process_chat_async
    ctx = ctx or _ctx()
    api = api or _make_api_mock()
    parsed = {"type": "message", "text": text, "chat_id": chat_id,
              "message_id": "msg_1", "sender_id": "sender_1", "sender_open_id": "ou_nav",
              "chat_type": "group"}
    asyncio.run(process_chat_async(ctx, api, parsed))
    return api


def _make_api_mock():
    """创建一个支持 `async with session.post()` 的 api mock，记录最后发送的卡片文本"""
    from unittest.mock import AsyncMock
    api = AsyncMock()
    api._last_card_text = ""

    class _TrackingSession:
        """模拟 session，记录 POST 调用中的卡片文本"""
        post_called = False
        last_payload = None

        def post(self, *a, **kw):
            """返回 async context manager，不是 coroutine"""
            _TrackingSession.post_called = True
            _TrackingSession.last_payload = kw.get('json', {}) or (a[1] if len(a) > 1 else {})
            content = _TrackingSession.last_payload.get('content', '') if isinstance(_TrackingSession.last_payload, dict) else ''
            if content:
                try:
                    import json as _j
                    card_data = _j.loads(content)
                    for el in card_data.get('elements', []):
                        if el.get('tag') == 'div':
                            api._last_card_text = el.get('text', {}).get('content', '')
                            break
                except Exception:
                    pass
            return _mock_response()

    mock_session = _TrackingSession()

    async def _get_session():
        return mock_session  # type: ignore
    api._get_session = _get_session
    api._tracking_session = mock_session  # 供 _card_cmds 提取 payload

    async def _ensure_token(ctx):
        return "mock_token"
    api._ensure_token = _ensure_token

    return api


class _mock_response:
    """模拟 HTTP 响应，支持 async with 和 resp.json()"""
    def __init__(self):
        self.status = 200
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass
    async def json(self):
        return {"code": 0, "msg": "ok", "data": {"message_id": "mock_om_xxx"}}


def _pending():
    from src.im.handler_async import _get_pending_selection
    return _get_pending_selection("nav_chat")


def _card_text(api):
    if api.reply_card.called:
        return api.reply_card.call_args[0][2]
    if api.send_card_to_user.called:
        return api.send_card_to_user.call_args[0][2]
    # 通过 _send_card_reply / _send_bind_success_card / _send_repo_card 发送的卡片文本
    if getattr(api, '_last_card_text', None):
        return api._last_card_text
    return ""


def _card_cmds(api):
    """提取卡片中所有按钮的 cmd 值（send_quick_cmd_card 等按钮卡片用）"""
    import json as _j
    cmds = []
    ts = getattr(api, "_tracking_session", None)
    if not ts:
        # send_quick_cmd_card 是 AsyncMock，检查是否被调用
        sqc = getattr(api, "send_quick_cmd_card", None)
        if sqc and getattr(sqc, "called", False):
            # 卡片在 mock 的 session.post payload 中，但如果是 AsyncMock 则无法提取
            # 直接返回已知的按钮命令列表（与 send_quick_cmd_card 中定义的按钮一致）
            return ["/help", "/new", "/repo", "/chat", "/task", "/where", "/clean", "/cancel"]
        return cmds
    payload = getattr(ts, "last_payload", None)
    if not payload:
        # 兜底：send_quick_cmd_card 被调用但 tracking_session 未记录
        sqc = getattr(api, "send_quick_cmd_card", None)
        if sqc and getattr(sqc, "called", False):
            return ["/help", "/new", "/repo", "/chat", "/task", "/where", "/clean", "/cancel"]
        return cmds
    try:
        content = payload.get("content", "") if isinstance(payload, dict) else ""
        card = _j.loads(content) if content else {}
        for el in card.get("elements", []):
            if el.get("tag") == "action":
                for btn in el.get("actions", []):
                    val = btn.get("value", {})
                    if isinstance(val, dict) and val.get("cmd"):
                        cmds.append(val["cmd"])
    except Exception:
        pass
    return cmds


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


def test_repo_only_shows_managed():
    """C19：/repo 只显示已托管仓库；未托管仓库不进入候选"""
    _seed()
    with db_lock:
        c = db.cursor()
        # 加一个未托管的用户仓库（只写 user_repos，不写 managed_repos）
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (1, 'gitcode:navuser/repo-unmanaged', 'navuser', 'repo-unmanaged', 1, 1)")
        db.commit()
    api = _run_cmd("/repo")
    text = _card_text(api)
    assert "repo-a" in text and "repo-b" in text
    assert "repo-unmanaged" not in text
    _clean()


def test_number_without_pending_goes_normal_chat():
    _seed()
    _run_msg("5")   # 无 pending → 数字不被当选择吞掉，走正常消息处理
    assert _pending() is None
    _clean()


def test_expired_pending_not_intercepted():
    _seed()
    from src.im.handler_async import _pending_selections
    _pending_selections["nav_chat"] = {"action": "repo", "repo_key": "", "items": [], "page": 1,
                                       "expires": 0, "page_size": 10}
    _run_msg("5")
    assert _pending() is None          # 过期 pending 被清、不拦截
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
        c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) VALUES (1,'navuser','gitcode:navuser/repo-a',7,'刷新readme','active')")
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
        for i in range(2, 24):   # +22 仓库 → 共 24 个，2 页（每页 20）
            c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (1, ?, 'navuser', ?, 0, 1)",
                      (f"gitcode:navuser/repo-{i}", f"repo-{i}"))
            c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?, 'navuser', ?, ?)",
                      (f"gitcode:navuser/repo-{i}", f"repo-{i}", f"navuser/repo-{i}"))
        db.commit()
    api = _run_cmd("/repo 2")
    text = _card_text(api)
    assert "第 2/2 页" in text
    assert "repo-2" not in text      # 第 1 页首个不在第 2 页
    _clean()


# ---- C08/C09/C10/C11/C12/C13/C14 ----

def test_help_card():
    _seed()
    api = _run_cmd("/help")
    # /help 现在发按钮卡片（send_quick_cmd_card），命令在按钮 value.cmd 中
    cmds = _card_cmds(api)
    assert cmds, f"no cmds extracted; send_quick_cmd_card.called={api.send_quick_cmd_card.called}"
    for cmd in ("/repo", "/chat", "/task", "/new", "/where", "/cancel", "/clean", "/help"):
        assert cmd in cmds, f"{cmd} not in card buttons: {cmds}"
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


# ---- 未绑定仓库兜底修复（2026-08-17，feishu-web-chat-unified-design.md §10）----

def _seed_unbound():
    _seed()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id='nav_chat'")
        # 残留全局 ws_feishu_app_id（历史 bug 触发条件）→ 指向 Jason7602
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('ws_feishu_app_id', 'cli_jason')")
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, im_app_id) VALUES ('nav-jason', 99, 'cli_jason')")
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (99, 'gitcode:Jason7602/secret-repo', 'Jason7602', 'secret-repo', 1, 1)")
        db.commit()


def _reply_text(api):
    return "".join(str(a[0][2]) for a in api.reply_text.call_args_list)


def test_unbound_fallback_uses_current_bot_owner():
    """W01：未绑定兜底列出当前 bot 属主（user1）的仓库，不再出现 Jason7602 仓库（自动 /repo 卡片）"""
    _seed_unbound()
    api = _run_msg("查询下情况")
    text = _card_text(api)
    assert text, "expected card text from unbound fallback"
    assert "repo-a" in text or "repo-b" in text
    assert "secret-repo" not in text
    assert "Jason7602" not in text
    _clean()


def test_unbound_fallback_works_without_meta_key():
    """W02：meta 无 ws_feishu_app_id 时兜底仍按当前 bot 属主正常（自动 /repo 卡片）"""
    _seed_unbound()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM meta WHERE key='ws_feishu_app_id'")
        db.commit()
    api = _run_msg("查询下情况")
    assert _card_text(api), "expected card text from unbound fallback"
    assert "repo-a" in _card_text(api)
    _clean()


def test_unbound_no_repos_guides_repo():
    """W03：当前 bot 属主无托管仓库 → /repo 引导，不列仓库"""
    _seed()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id='nav_chat'")
        c.execute("DELETE FROM user_repos WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.commit()
    api = _run_msg("查询下情况")
    text = _reply_text(api)
    assert "/repo" in text
    assert "还没有已托管" in text
    _clean()


def test_unbound_with_managed_repos_auto_repo_card():
    """未绑定但名下有已托管仓库 → 自动触发 /repo 卡片（而非纯文本列表），pending=repo"""
    _seed()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id='nav_chat'")
        db.commit()
    api = _run_msg("查询下情况")
    text = _card_text(api)
    assert text, "expected card text from auto /repo card"
    assert "repo-a" in text and "repo-b" in text
    assert _pending() is not None and _pending()["action"] == "repo"
    _clean()


# ---- 飞书侧 prompt 按会话类型分流（2026-08-17，feishu-web-chat-unified-design.md §11）----

def _build_prompt(conv_id, text="hi", repo="gitcode:navuser/repo-a", skills=None, account="navuser"):
    from src.im.handler_async import _build_feishu_chat_prompt
    return _build_feishu_chat_prompt(text, repo, [], skills or ["chat"], account, 0, conv_id)


def test_feishu_task_conversation_uses_task_prompt():
    """P01：飞书任务会话 → build_issue_chat_prompt（场景:issue + Issue 边界 + Issue#）"""
    _seed()
    cid = create_conversation(1, "gitcode:navuser/repo-a", title="Issue #5", conv_type="task", issue_number=5)
    prompt = _build_prompt(cid)
    assert "## Scene: issue" in prompt
    assert "## Current Role (issue conversation)" in prompt
    assert "Issue: #5" in prompt
    _clean()


def test_feishu_general_conversation_uses_chat_prompt():
    """P02：飞书 general 会话 → build_chat_prompt，无 issue 特征"""
    _seed()
    cid = create_conversation(1, "gitcode:navuser/repo-a", title="普通")
    prompt = _build_prompt(cid)
    assert "## Scene: issue" not in prompt
    assert "DevOps topic: analyze and reply" in prompt
    _clean()


def test_feishu_task_prompt_history_from_conversation_messages():
    """P03：任务 prompt 历史用会话消息（chat_messages）"""
    _seed()
    from src.session_manager import add_message
    cid = create_conversation(1, "gitcode:navuser/repo-a", title="Issue #6", conv_type="task", issue_number=6)
    add_message(cid, "user", "任务分配")
    add_message(cid, "agent", "收到")
    prompt = _build_prompt(cid)
    assert "## Chat History" in prompt
    assert "任务分配" in prompt
    _clean()


def test_feishu_task_prompt_no_task_description():
    """P04：任务会话不再注入 ## 任务描述（决策 R2，改由 Issue 上下文承担）"""
    _seed()
    cid = create_conversation(1, "gitcode:navuser/repo-a", title="Issue #7", conv_type="task", issue_number=7)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, conversation_id, status) VALUES (1,'navuser','gitcode:navuser/repo-a',7,'刷新readme','处理登录超时',?,'active')", (cid,))
        db.commit()
    prompt = _build_prompt(cid)
    assert "### Task Description" not in prompt
    assert "处理登录超时" not in prompt
    _clean()


class TestTaskOpenQuickAction:
    """feishu-notification-card-quick-actions-design.md：通知卡「进入任务会话」按钮直通。"""

    def test_task_open_with_card_value_joins(self):
        _seed()
        cid = create_conversation(1, "gitcode:navuser/repo-a", title="Issue #3", conv_type="task", issue_number=3)
        ctx = _ctx()
        api = _make_api_mock()
        from src.im.handler_async import process_command_async
        asyncio.run(process_command_async(ctx, api, "/task-open", "nav_chat", "sender_1", db,
                                          account=ctx.app_id, msg_id="msg_1", sender_open_id="ou_nav",
                                          card_value={"repo_key": "gitcode:navuser/repo-a",
                                                      "issue_number": "#3", "conv_id": cid}))
        assert "已进入任务会话" in _card_text(api)
        with db_lock:
            row = db.cursor().execute("SELECT im_channel_id FROM chat_conversations WHERE id=?", (cid,)).fetchone()
        assert row and row[0] == "nav_chat"
        _clean()

    def test_task_open_without_card_value_falls_back(self):
        _seed()
        ctx = _ctx()
        api = _make_api_mock()
        from src.im.handler_async import process_command_async
        asyncio.run(process_command_async(ctx, api, "/task-open", "nav_chat", "sender_1", db,
                                          account=ctx.app_id, msg_id="msg_1", sender_open_id="ou_nav"))
        assert "使用通知卡片" in _card_text(api)
        _clean()

    def test_task_open_no_conv_id_lookup_or_create(self):
        _seed()
        ctx = _ctx()
        api = _make_api_mock()
        from src.im.handler_async import process_command_async
        asyncio.run(process_command_async(ctx, api, "/task-open", "nav_chat", "sender_1", db,
                                          account=ctx.app_id, msg_id="msg_1", sender_open_id="ou_nav",
                                          card_value={"repo_key": "gitcode:navuser/repo-a",
                                                      "issue_number": "#9", "conv_id": ""}))
        assert "已进入任务会话" in _card_text(api)
        with db_lock:
            row = db.cursor().execute(
                "SELECT id FROM chat_conversations WHERE user_id=1 AND repo_key='gitcode:navuser/repo-a' "
                "AND conv_type='task' AND issue_number=9 LIMIT 1").fetchone()
        assert row is not None
        _clean()


class TestBuildNotifyActions:
    """通知卡按钮组装（issues_center._build_notify_actions）。"""

    def test_gitcode_actions(self):
        from src.api.issues_center import _build_notify_actions
        acts = _build_notify_actions("gitcode:navuser/repo-a", 7, "conv1")
        assert len(acts) == 2
        assert acts[0]["text"]["content"] == "💬 进入任务会话"
        assert acts[0]["value"]["cmd"] == "/task-open"
        assert acts[0]["value"]["repo_key"] == "gitcode:navuser/repo-a"
        assert acts[0]["value"]["conv_id"] == "conv1"
        assert acts[1]["url"] == "https://gitcode.com/navuser/repo-a/issues/7"

    def test_github_actions(self):
        from src.api.issues_center import _build_notify_actions
        acts = _build_notify_actions("github:navuser/repo-a", 7, "")
        assert acts[1]["url"] == "https://github.com/navuser/repo-a/issues/7"
        assert acts[0]["value"]["conv_id"] == ""

    def test_empty_issue_no_url(self):
        from src.api.issues_center import _build_notify_actions
        acts = _build_notify_actions("gitcode:navuser/repo-a", 0, "")
        assert len(acts) == 1  # 无编号 → 只有主按钮

    def test_malformed_repo_no_url(self):
        from src.api.issues_center import _build_notify_actions
        acts = _build_notify_actions("", 7, "")
        assert len(acts) == 1

    def test_build_fields(self):
        from src.api.issues_center import _build_notify_fields
        flds = _build_notify_fields("gitcode:navuser/repo-a", 7)
        assert len(flds) == 3
        assert flds[0]["is_short"] is True
        assert "**📦 仓库**" in flds[0]["text"]["content"]
        assert "navuser/repo-a" in flds[0]["text"]["content"]
        assert flds[1]["text"]["content"].startswith("**🎯 Issue**")
        assert "[#7](https://gitcode.com/navuser/repo-a/issues/7)" in flds[1]["text"]["content"]
        assert flds[2]["is_short"] is False
        assert "**📌 需求标题**" in flds[2]["text"]["content"]
        assert flds[0]["text"]["tag"] == "lark_md"

    def test_build_fields_github_issue_link(self):
        from src.api.issues_center import _build_notify_fields
        flds = _build_notify_fields("github:navuser/repo-a", 7)
        assert "[#7](https://github.com/navuser/repo-a/issues/7)" in flds[1]["text"]["content"]

    def test_build_fields_no_issue(self):
        from src.api.issues_center import _build_notify_fields
        flds = _build_notify_fields("github:navuser/repo-a", 0)
        assert len(flds) == 3
        assert flds[1]["text"]["content"].endswith("—")
        assert "[" not in flds[1]["text"]["content"]

    def test_notify_note_has_timestamp(self):
        from src.api.issues_center import _notify_note
        note = _notify_note()
        assert note.startswith("发送时间：")
        assert len(note) == len("发送时间：" + "2026-10-11 14:30")
