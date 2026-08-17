"""Tests for p2p confirmation-based feishu mapping binding."""
import pytest, os, sys
from unittest.mock import AsyncMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


@pytest.fixture(autouse=True)
def _clean_mapping():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user LIKE 'p2puser%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user LIKE 'p2puser%'")
        db.commit()


def _make_ctx(app_id="cli_p2ptest"):
    from src.im.feishu_async import BotContext
    ctx = BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="p2p-test")
    return ctx


def _seed_user_and_bot(username, app_id, user_id):
    """创建 user_accounts 用户 + chat_agent_session 关联 app_id（用真实 user id）"""
    from src.user import db as user_db
    from src.session_manager import db, db_lock
    user = user_db.get_user_by_username(username) or user_db.create_user(username, "pw")
    real_uid = user["id"]
    acc = user_db.get_user_account(username) or {}
    acc["nickname"] = "测试昵称"
    user_db.save_user_account(username, acc)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_agent_session WHERE im_app_id=?", (app_id,))
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_provider) VALUES (?,?,?,?,?)",
                  (f"sess-{app_id}", real_uid, "", app_id, "feishu"))
        db.commit()
    return user


async def _send(parsed, app_id="cli_p2ptest"):
    from src.im.handler_async import process_chat_async
    ctx = _make_ctx(app_id)
    api = AsyncMock()
    await process_chat_async(ctx, api, parsed)
    return api


def test_first_p2p_message_asks_confirmation_no_bind():
    """首次私聊消息：发确认询问，不直接绑定"""
    import asyncio
    _seed_user_and_bot("p2puser1", "cli_p2p1", 70001)
    api = asyncio.run(_send({
        "type": "message", "text": "你好", "chat_id": "oc_x",
        "chat_type": "p2p", "sender_open_id": "ou_p2p1", "message_id": "m1",
    }, app_id="cli_p2p1"))
    api.reply_text.assert_called_once()
    ask_text = api.reply_text.call_args[0][2]
    assert "GitCode" in ask_text or "确认" in ask_text or "昵称" in ask_text
    # 未绑定
    from src.session_manager import get_feishu_open_id
    assert get_feishu_open_id("p2puser1") is None


def test_confirm_binds_mapping():
    """回复「是」后绑定映射"""
    import asyncio
    _seed_user_and_bot("p2puser2", "cli_p2p2", 70002)
    # 首次消息建立 pending
    asyncio.run(_send({
        "type": "message", "text": "hi", "chat_id": "oc_x",
        "chat_type": "p2p", "sender_open_id": "ou_p2p2", "message_id": "m1",
    }, app_id="cli_p2p2"))
    # 回复"是"
    api2 = asyncio.run(_send({
        "type": "message", "text": "是", "chat_id": "oc_x",
        "chat_type": "p2p", "sender_open_id": "ou_p2p2", "message_id": "m2",
    }, app_id="cli_p2p2"))
    api2.reply_text.assert_called()
    from src.session_manager import get_feishu_open_id, get_feishu_mapping, db, db_lock
    with db_lock:
        _rows = db.cursor().execute("SELECT user_id, gitcode_user, feishu_open_id FROM unified_user_mapping").fetchall()
    assert _rows, f"mapping table empty after confirm; replies={[c.args for c in api2.reply_text.call_args_list]}"
    # 已绑定
    assert get_feishu_open_id("p2puser2") == "ou_p2p2"
    mapping = get_feishu_mapping("")
    assert mapping["p2puser2"]["gitcode_nickname"] == "测试昵称"


def test_deny_does_not_bind():
    """回复「不是」不绑定"""
    import asyncio
    _seed_user_and_bot("p2puser3", "cli_p2p3", 70003)
    asyncio.run(_send({
        "type": "message", "text": "hi", "chat_id": "oc_x",
        "chat_type": "p2p", "sender_open_id": "ou_p2p3", "message_id": "m1",
    }, app_id="cli_p2p3"))
    api2 = asyncio.run(_send({
        "type": "message", "text": "不是", "chat_id": "oc_x",
        "chat_type": "p2p", "sender_open_id": "ou_p2p3", "message_id": "m2",
    }, app_id="cli_p2p3"))
    api2.reply_text.assert_called()
    from src.session_manager import get_feishu_open_id
    assert get_feishu_open_id("p2puser3") is None


def test_group_message_does_not_trigger_bind():
    """群聊消息不触发绑定"""
    import asyncio
    _seed_user_and_bot("p2puser4", "cli_p2p4", 70004)
    api = asyncio.run(_send({
        "type": "message", "text": "你好", "chat_id": "oc_g",
        "chat_type": "group", "sender_open_id": "ou_p2p4", "message_id": "m1",
    }, app_id="cli_p2p4"))
    # 群聊不应发确认消息（reply_text 可能被后续逻辑调用，此处断言不出现确认文案）
    from src.session_manager import get_feishu_open_id
    assert get_feishu_open_id("p2puser4") is None
    calls = [c.args[2] for c in api.reply_text.call_args_list if len(c.args) > 2]
    assert not any("GitCode" in t or "确认" in t for t in calls)


def test_already_bound_no_requery():
    """已绑定后不再发确认询问"""
    import asyncio
    from src.session_manager import set_feishu_open_id
    _seed_user_and_bot("p2puser5", "cli_p2p5", 70005)
    set_feishu_open_id("", "p2puser5", "ou_existing")
    api = asyncio.run(_send({
        "type": "message", "text": "你好", "chat_id": "oc_x",
        "chat_type": "p2p", "sender_open_id": "ou_existing", "message_id": "m1",
    }, app_id="cli_p2p5"))
    calls = [c.args[2] for c in api.reply_text.call_args_list if len(c.args) > 2]
    assert not any("GitCode" in t or "确认" in t for t in calls)
    from src.session_manager import get_feishu_open_id
    assert get_feishu_open_id("p2puser5") == "ou_existing"


def test_oauth_login_empty_open_id_preserves_binding():
    """OAuth 登录传空 open_id 不得覆盖已有飞书绑定（2026-08-16 修复）。

    回归：此前 auth.py OAuth 登录调 set_feishu_open_id(open_id='') 用 INSERT OR REPLACE
    把 confirmed 的 feishu_open_id 清空，导致系统反复要求重新确认绑定。修复后空 open_id
    应保留已有 open_id，仅刷新昵称。
    """
    import asyncio
    from src.session_manager import set_feishu_open_id, get_feishu_open_id, db, db_lock
    _seed_user_and_bot("p2puser6", "cli_p2p6", 70006)
    # 1) 飞书确认绑定 open_id
    set_feishu_open_id("", "p2puser6", "ou_p2p6_bind", feishu_name="飞书昵称", gitcode_nickname="旧昵称")
    assert get_feishu_open_id("p2puser6") == "ou_p2p6_bind"
    # 2) 模拟 OAuth 登录：只带昵称，open_id 为空
    set_feishu_open_id("", "p2puser6", "", "", gitcode_nickname="新昵称")
    # 3) open_id 必须保留，昵称刷新
    assert get_feishu_open_id("p2puser6") == "ou_p2p6_bind", "空 open_id 不应覆盖已有绑定"
    with db_lock:
        row = db.cursor().execute(
            "SELECT feishu_open_id, gitcode_nickname, feishu_name FROM unified_user_mapping WHERE gitcode_user='p2puser6'").fetchone()
    assert row and row[0] == "ou_p2p6_bind"
    assert row[1] == "新昵称", "昵称应刷新"
    assert row[2] == "飞书昵称", "空 feishu_name 不应覆盖已有 feishu_name"


def test_set_feishu_open_id_empty_no_existing_creates_without_open_id():
    """无已有绑定时，空 open_id 写入映射（不报错，open_id 留空）——OAuth 首次登录建行"""
    import asyncio
    from src.session_manager import set_feishu_open_id, get_feishu_open_id
    _seed_user_and_bot("p2puser7", "cli_p2p7", 70007)
    set_feishu_open_id("", "p2puser7", "", "", gitcode_nickname="昵称7")
    assert get_feishu_open_id("p2puser7") in (None, ""), "无绑定时空 open_id 不产生 open_id"
