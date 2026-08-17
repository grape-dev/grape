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


# ---- asyncio.Lock 死锁修复（feishu-chat-event-loop-deadlock-fix.md §5.1 D01-D08）----

def test_repo_lock_is_async_singleton_per_repo():
    """D01/D02：acquire_repo_lock 返回 asyncio.Lock；同仓库同一实例，不同仓库不同实例"""
    from src.api.event_store import acquire_repo_lock
    l1 = acquire_repo_lock("dl-r1")
    l2 = acquire_repo_lock("dl-r1")
    l3 = acquire_repo_lock("dl-r2")
    assert isinstance(l1, asyncio.Lock)
    assert l1 is l2
    assert l1 is not l3


def test_same_repo_serialized_concurrent():
    """D03：同仓库两任务持锁串行，执行区间不重叠"""
    from src.api.event_store import acquire_repo_lock
    lock = acquire_repo_lock("dl-serial")
    seq = []

    async def work(name, delay):
        async with lock:
            seq.append(("start", name))
            await asyncio.sleep(delay)
            seq.append(("end", name))

    async def main():
        await asyncio.gather(work("A", 0.05), work("B", 0.05))

    asyncio.run(main())
    order = [x[0] for x in seq]
    assert order == ["start", "end", "start", "end"]


def test_event_loop_stays_responsive_while_contended():
    """D04：同仓库持锁期间事件循环不被阻塞（心跳持续），等锁协程最终进入——事故最小复现"""
    from src.api.event_store import acquire_repo_lock
    lock = acquire_repo_lock("dl-resp")
    ticks = []

    async def heartbeat():
        for _ in range(30):
            ticks.append(1)
            await asyncio.sleep(0.01)

    async def holder():
        async with lock:
            await asyncio.sleep(0.2)

    async def waiter():
        async with lock:
            ticks.append(999)

    async def main():
        await asyncio.gather(holder(), heartbeat(), waiter())

    asyncio.run(main())
    assert len(ticks) > 15          # 心跳在等锁期间持续递增 → 循环未被占死
    assert 999 in ticks             # 等锁协程最终拿到锁并执行


def test_different_repos_parallel():
    """D05：不同仓库并行不互斥"""
    import time as _t
    from src.api.event_store import acquire_repo_lock
    l1 = acquire_repo_lock("dl-para-a")
    l2 = acquire_repo_lock("dl-para-b")

    async def work(lock):
        async with lock:
            await asyncio.sleep(0.1)

    async def main():
        t0 = _t.monotonic()
        await asyncio.gather(work(l1), work(l2))
        return _t.monotonic() - t0

    el = asyncio.run(main())
    assert el < 0.19  # 并行 ≈0.1s，而非串行 0.2s


def test_lock_released_on_cancel():
    """D06：协程取消后锁可被后续任务获取（无泄漏）"""
    from src.api.event_store import acquire_repo_lock
    lock = acquire_repo_lock("dl-cancel")
    got = []

    async def canceller():
        async with lock:
            raise asyncio.CancelledError

    async def main():
        c = asyncio.create_task(canceller())
        await asyncio.sleep(0.01)
        c.cancel()
        await asyncio.gather(c, return_exceptions=True)
        async with lock:
            got.append(True)

    asyncio.run(main())
    assert got == [True]
