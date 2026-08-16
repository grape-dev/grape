"""飞书 Chat 与 Web Chat 统一：join_feishu_chat 指针切换、共享消息存储、web→飞书推送守卫。

见 docs/new-version2/feishu-web-chat-unified-design.md §3 与 §6.2（T01-T16）。
"""
import os
import sys
import threading
import asyncio
from types import SimpleNamespace

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import (
    db, db_lock,
    create_conversation, get_conversation, get_conversation_by_im,
    update_conversation, add_message, get_messages,
    join_feishu_chat,
)


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_conversations")
        c.execute("DELETE FROM chat_messages")
        db.commit()


def _mk_conv(user_id, repo="owner/repo", title="", conv_type="general", issue_number=0):
    return create_conversation(user_id, repo, title=title, conv_type=conv_type, issue_number=issue_number)


# ---- T01/T02/T03/T06/T07：join_feishu_chat 指针切换 ----

def test_join_first_binding_sets_pointer():
    _clean()
    cid = _mk_conv(1, title="A")
    assert join_feishu_chat("ou_111", cid, 1) is True
    assert get_conversation(cid)["im_channel_id"] == "ou_111"
    _clean()


def test_join_switches_pointer_atomically():
    _clean()
    a = _mk_conv(1, title="A")
    b = _mk_conv(1, title="B")
    join_feishu_chat("ou_111", a, 1)
    assert join_feishu_chat("ou_111", b, 1) is True
    assert get_conversation(a)["im_channel_id"] == ""
    assert get_conversation(b)["im_channel_id"] == "ou_111"
    _clean()


def test_join_same_conversation_idempotent():
    _clean()
    a = _mk_conv(1, title="A")
    join_feishu_chat("ou_111", a, 1)
    assert join_feishu_chat("ou_111", a, 1) is True
    assert get_conversation(a)["im_channel_id"] == "ou_111"
    _clean()


def test_join_invalid_conversation_rejected():
    _clean()
    assert join_feishu_chat("ou_111", "not-exist", 1) is False
    _clean()


def test_join_other_user_conversation_rejected():
    _clean()
    cid = _mk_conv(2, title="other")
    assert join_feishu_chat("ou_111", cid, 1) is False
    assert get_conversation(cid)["im_channel_id"] == ""
    _clean()


def test_join_concurrent_switch_single_holder():
    """T04 并发 join：终态只有一条 conversation 持有 chat_id（db_lock 保证）"""
    _clean()
    b = _mk_conv(1, title="B")
    c = _mk_conv(1, title="C")
    errs = []

    def _join(cid):
        try:
            join_feishu_chat("ou_111", cid, 1)
        except Exception as e:
            errs.append(e)

    t1 = threading.Thread(target=_join, args=(b,))
    t2 = threading.Thread(target=_join, args=(c,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errs
    holders = [cv for cv in (b, c) if get_conversation(cv)["im_channel_id"] == "ou_111"]
    assert len(holders) == 1
    _clean()


def test_update_conversation_allows_im_channel_id():
    _clean()
    cid = _mk_conv(1)
    update_conversation(cid, im_channel_id="ou_222")
    assert get_conversation(cid)["im_channel_id"] == "ou_222"
    _clean()


# ---- T09/T10：共享消息存储 ----

def test_shared_message_storage_roundtrip():
    _clean()
    cid = _mk_conv(1, repo="owner/repo")
    add_message(cid, "user", "飞书消息")
    add_message(cid, "agent", "agent 答复")
    msgs = get_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "agent"]
    assert msgs[1]["content"] == "agent 答复"
    _clean()


def test_get_conversation_by_im_returns_current_pointer():
    _clean()
    a = _mk_conv(1, title="A")
    b = _mk_conv(1, title="B")
    join_feishu_chat("ou_111", a, 1)
    assert get_conversation_by_im("ou_111", 1)["id"] == a
    join_feishu_chat("ou_111", b, 1)
    assert get_conversation_by_im("ou_111", 1)["id"] == b
    _clean()


def test_conversation_list_carries_type_and_issue():
    """T05 数据侧：get_user_conversations 标注 conv_type/issue_number，可区分任务/一般对话"""
    _clean()
    cid = _mk_conv(1, title="Issue #5", conv_type="task", issue_number=5)
    from src.session_manager import get_user_conversations
    convs = get_user_conversations(1, repo_key="owner/repo")
    assert any(c["id"] == cid and c["conv_type"] == "task" and c["issue_number"] == 5 for c in convs)
    _clean()


# ---- T11/T12/T13：web→飞书推送守卫与发送 ----

def _import_push():
    from src.api.routes.chat import _push_reply_to_feishu
    return _push_reply_to_feishu


def test_push_noop_when_conv_not_bound():
    """T11/T13：conversation 未绑定飞书（im_channel_id 空）→ 不推送、无副作用"""
    _clean()
    cid = _mk_conv(1)
    conv = get_conversation(cid)
    asyncio.run(_import_push()(conv, {"id": 1, "username": "alice"}, "reply"))
    _clean()


def test_push_noop_when_no_bot_manager():
    _clean()
    from src.api import main as api_main
    cid = _mk_conv(1)
    update_conversation(cid, im_channel_id="ou_111")
    conv = get_conversation(cid)
    saved = getattr(api_main, "bot_manager", None)
    api_main.bot_manager = None
    try:
        asyncio.run(_import_push()(conv, {"id": 1, "username": "alice"}, "reply"))
    finally:
        api_main.bot_manager = saved
    _clean()


def test_push_noop_when_no_open_id():
    _clean()
    from src.api import main as api_main
    cid = _mk_conv(1)
    update_conversation(cid, im_channel_id="oc_group_123")  # 群聊 chat_id，且用户无 open_id
    conv = get_conversation(cid)
    saved = getattr(api_main, "bot_manager", None)
    api_main.bot_manager = None
    try:
        asyncio.run(_import_push()(conv, {"id": 1, "username": "alice"}, "reply"))
    finally:
        api_main.bot_manager = saved
    _clean()


class _FakeApi:
    def __init__(self):
        self.calls = []

    async def send_card_to_user(self, ctx, open_id, text, title=""):
        self.calls.append((ctx, open_id, text, title))
        return "msg_id"


class _FakeBotManager:
    def __init__(self, app_id):
        self.api = _FakeApi()
        self._app_id = app_id

    def get_bot(self, app_id):
        return SimpleNamespace(app_id=app_id) if app_id == self._app_id else None


def _seed_feishu_binding(user_id, gitcode_user, open_id, app_id):
    """写入 unified_user_mapping + chat_agent_session（供 _push_reply_to_feishu 解析）"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id) VALUES (?,?,?)",
            (user_id, gitcode_user, open_id))
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session (id, user_id, im_app_id) VALUES (?,?,?)",
            (f"test-{app_id}", user_id, app_id))
        db.commit()


def _clean_feishu_binding(user_id, gitcode_user, app_id):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (gitcode_user,))
        c.execute("DELETE FROM chat_agent_session WHERE user_id=?", (user_id,))
        db.commit()


def test_push_sends_card_to_feishu():
    """T12：conversation 已绑定 + 用户有 open_id + 有 chat_bot → send_card_to_user 被调用（带 [来自Web] 前缀）"""
    _clean()
    from src.api import main as api_main
    _seed_feishu_binding(1, "alice", "ou_111", "cli_test_app")
    cid = _mk_conv(1, title="A")
    update_conversation(cid, im_channel_id="ou_111")
    conv = get_conversation(cid)
    fake_bm = _FakeBotManager("cli_test_app")
    saved = getattr(api_main, "bot_manager", None)
    api_main.bot_manager = fake_bm
    try:
        asyncio.run(_import_push()(conv, {"id": 1, "username": "alice"}, "你好"))
    finally:
        api_main.bot_manager = saved
        _clean_feishu_binding(1, "alice", "cli_test_app")
    assert len(fake_bm.api.calls) == 1
    ctx, open_id, text, title = fake_bm.api.calls[0]
    assert open_id == "ou_111"
    assert text.startswith("[来自Web]")
    _clean()


# ---- token 刷新：会话缓存旧 token 导致 401（2026-08-16 修复）----

def _import_resolve():
    from src.api.routes.chat import _resolve_gitcode_token
    return _resolve_gitcode_token


def test_resolve_prefers_current_user_token_and_refreshes_cache():
    """会话缓存旧 token + 用户有当前 token → 用当前 token 且刷新会话缓存"""
    _clean()
    cid = _mk_conv(1, title="A")
    update_conversation(cid, user_gitcode_token="STALE_OLD_TOKEN")
    conv = get_conversation(cid)
    tok = _import_resolve()(conv, cid, "FRESH_NEW_TOKEN")
    assert tok == "FRESH_NEW_TOKEN"
    assert get_conversation(cid)["user_gitcode_token"] == "FRESH_NEW_TOKEN"
    _clean()


def test_resolve_falls_back_to_cached_when_no_user_token():
    _clean()
    cid = _mk_conv(1, title="A")
    update_conversation(cid, user_gitcode_token="CACHED_TOKEN")
    conv = get_conversation(cid)
    tok = _import_resolve()(conv, cid, "")
    assert tok == "CACHED_TOKEN"
    _clean()


def test_resolve_returns_user_token_when_conv_none():
    _clean()
    tok = _import_resolve()(None, "", "USER_TOKEN")
    assert tok == "USER_TOKEN"
    _clean()
