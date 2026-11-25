"""Tests for p2p confirmation-based feishu mapping binding."""
import pytest, os, sys
from unittest.mock import AsyncMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


def _clean_p2p_rows():
    """清理本文件造的用户/映射/渠道身份/安装行，避免重复运行互相污染。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM grape_accounts "
                  "WHERE username LIKE 'p2puser%' OR username LIKE 'wxuser%'")
        for (uid,) in c.fetchall():
            if uid:
                c.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (uid,))
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user LIKE 'p2puser%' "
                  "OR gitcode_user LIKE 'wxuser%'")
        c.execute("DELETE FROM grape_t_channel_sender_identity "
                  "WHERE platform_user_id LIKE 'ou_p2p%' OR platform_user_id LIKE 'wx_%'")
        c.execute("DELETE FROM grape_t_channel_installation "
                  "WHERE legacy_app_id LIKE 'cli_p2p%' OR legacy_app_id LIKE 'cli_wx%'")
        c.execute("DELETE FROM chat_agent_session "
                  "WHERE id LIKE 'sess-cli_p2p%' OR id LIKE 'sess-cli_wx%'")
        db.commit()


@pytest.fixture(autouse=True)
def _clean_mapping():
    _clean_p2p_rows()
    yield
    _clean_p2p_rows()


def _make_ctx(app_id="cli_p2ptest"):
    from src.domains.runtime.im.feishu_async import BotContext
    ctx = BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="p2p-test")
    return ctx


def _seed_user_and_bot(username, app_id, user_id, channel_type="feishu"):
    """创建 grape_accounts 用户 + chat_agent_session 关联 app_id（用真实 user id）"""
    from src.kernel.session_manager import db, db_lock
    from src.domains.mgmt.auth.password import hash_password
    # D105: grape_accounts.id 为 TEXT 主键，显式写入避免 NULL 主键；
    # 用整型字符串以兼容 unified_user_mapping.user_id INTEGER 主键（存量形态）。
    real_uid = str(user_id)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM grape_accounts WHERE username=?", (username,))
        row = c.fetchone()
        if row:
            c.execute("UPDATE grape_accounts SET id=?, nickname=? WHERE username=?",
                      (real_uid, "测试昵称", username))
        else:
            c.execute("INSERT INTO grape_accounts (id, username, email, password_hash, nickname, status, token_version) VALUES (?,?,?,?,?,1,0)",
                      (real_uid, username, None, hash_password("pw"), "测试昵称"))
        db.commit()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id=?", (app_id,))
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key) VALUES (?,?, '')",
                  (f"sess-{app_id}", real_uid))
        db.commit()
    # A2 Phase B：bot → 用户 的归属看 grape_t_channel_installation（user 轨 owner）
    from src.domains.runtime.im.engine_wiring import sync_installation
    sync_installation("user", owner_user_id=str(real_uid), app_id=app_id,
                      app_secret="sec", channel_type=channel_type)
    return {"id": real_uid, "username": username}


async def _send(parsed, app_id="cli_p2ptest", channel_type=""):
    from src.domains.runtime.im.handler_async import process_chat_async
    ctx = _make_ctx(app_id)
    api = AsyncMock()
    if channel_type and "channel_type" not in parsed:
        parsed = {**parsed, "channel_type": channel_type}
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
    from src.kernel.session_manager import get_feishu_open_id
    assert get_feishu_open_id("p2puser1") is None


def test_confirm_binds_mapping():
    """回复「是」后绑定映射"""
    import asyncio
    _user_info = _seed_user_and_bot("p2puser2", "cli_p2p2", 70002)
    _uid = _user_info["id"]
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
    from src.kernel.session_manager import get_feishu_open_id, get_feishu_mapping, db, db_lock
    with db_lock:
        _rows = db.cursor().execute("SELECT user_id, gitcode_user, feishu_open_id FROM unified_user_mapping").fetchall()
    assert _rows, f"mapping table empty after confirm; replies={[c.args for c in api2.reply_text.call_args_list]}"
    # 已绑定（FR-22.5: bind_feishu_identity 只写飞书三列不写 gitcode_user，
    # get_feishu_open_id 通过 user_id 回退命中）
    assert get_feishu_open_id("p2puser2") == "ou_p2p2"
    # FR-18.7: bind_feishu_identity 不写 gitcode_user 列，直接查 um 行验证
    with db_lock:
        _um = db.cursor().execute("SELECT feishu_open_id, feishu_name FROM unified_user_mapping WHERE user_id=?", (_uid,)).fetchone()
    assert _um and _um[0] == "ou_p2p2"
    # 飞书渠道回执保持原文
    replies = [c.args[2] for c in api2.reply_text.call_args_list if len(c.args) > 2]
    assert any("已绑定当前飞书账号" in t for t in replies)


def test_wechat_p2p_confirm_uses_channel_and_skips_feishu_identity():
    """Issue #3475：微信 p2p 确认回执按渠道渲染，且不写 unified_user_mapping.feishu_open_id。"""
    import asyncio
    info = _seed_user_and_bot("wxuser1", "cli_wx1", 71001, channel_type="wechat")
    uid = info["id"]
    asyncio.run(_send({
        "type": "message", "text": "hi", "chat_id": "wx_chat",
        "chat_type": "p2p", "sender_open_id": "wx_sender_1", "message_id": "m1",
    }, app_id="cli_wx1", channel_type="wechat"))
    api2 = asyncio.run(_send({
        "type": "message", "text": "是", "chat_id": "wx_chat",
        "chat_type": "p2p", "sender_open_id": "wx_sender_1", "message_id": "m2",
    }, app_id="cli_wx1", channel_type="wechat"))
    replies = [c.args[2] for c in api2.reply_text.call_args_list if len(c.args) > 2]
    confirm = next(t for t in replies if "已确认" in t)
    assert "微信" in confirm and "飞书" not in confirm
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        um = db.cursor().execute(
            "SELECT feishu_open_id FROM unified_user_mapping WHERE user_id=?", (uid,)).fetchone()
        sid = db.cursor().execute(
            "SELECT user_id FROM grape_t_channel_sender_identity WHERE platform_user_id=?",
            ("wx_sender_1",)).fetchone()
    assert not um or um[0] != "wx_sender_1", "微信 sender id 不得写入飞书身份列"
    assert sid and str(sid[0]) == str(uid)


def test_wechat_unbound_hint_points_to_channel_not_feishu():
    """Issue #3475：非飞书渠道未绑定提示不得硬编码飞书。"""
    import asyncio
    from src.domains.runtime.im.handler_async import process_command_async
    from src.kernel.session_manager import db
    ctx = _make_ctx("cli_wx_nouser")
    api = AsyncMock()
    result = asyncio.run(process_command_async(
        ctx, api, "/repo", "wx_chat", "wx_sender_x", db, account=ctx.app_id,
        msg_id="m1", sender_open_id="wx_sender_x", channel_type="wechat"))
    assert "微信" in result and "飞书" not in result


def test_wechat_bind_succeeds_when_repo_row_updated_later():
    """Issue #3475 复审：同 app 同时存在 repo/user 安装行、且 repo 行 updated_at 更新
（installation_list_all 按 updated_at 倒序 → repo 行排在首位）时，微信 p2p 确认绑定
必须跳过不匹配的 repo 行继续扫描（continue），落到 user 轨 owner，而不是恒失败。"""
    import asyncio
    from src.kernel.session_manager import db, db_lock
    info = _seed_user_and_bot("wxuser2", "cli_wx2", 71002, channel_type="wechat")
    uid = info["id"]
    # 同 app 追加 repo 轨安装，并把 updated_at 拨到最新 → DESC 排序后成为首行
    from src.domains.runtime.im.engine_wiring import sync_installation
    assert sync_installation("repo", repo_key="gitcode:acme/wxdual",
                             app_id="cli_wx2", app_secret="sec",
                             channel_type="wechat") is True
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE grape_t_channel_installation "
                  "SET updated_at=datetime('now','+1 hour') "
                  "WHERE scope='repo' AND legacy_app_id='cli_wx2'")
        db.commit()
        first = c.execute("SELECT scope FROM grape_t_channel_installation "
                          "WHERE legacy_app_id='cli_wx2' "
                          "ORDER BY updated_at DESC LIMIT 1").fetchone()
    assert first and first[0] == "repo", "测试前置失败：repo 行必须排在列表首位"
    asyncio.run(_send({
        "type": "message", "text": "hi", "chat_id": "wx_chat2",
        "chat_type": "p2p", "sender_open_id": "wx_sender_2", "message_id": "m1",
    }, app_id="cli_wx2", channel_type="wechat"))
    api2 = asyncio.run(_send({
        "type": "message", "text": "是", "chat_id": "wx_chat2",
        "chat_type": "p2p", "sender_open_id": "wx_sender_2", "message_id": "m2",
    }, app_id="cli_wx2", channel_type="wechat"))
    replies = [c.args[2] for c in api2.reply_text.call_args_list if len(c.args) > 2]
    assert any("已绑定当前微信账号" in t for t in replies), replies
    assert not any("绑定失败" in t for t in replies), replies
    with db_lock:
        sid = db.cursor().execute(
            "SELECT user_id FROM grape_t_channel_sender_identity WHERE platform_user_id=?",
            ("wx_sender_2",)).fetchone()
    assert sid and str(sid[0]) == str(uid)


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
    from src.kernel.session_manager import get_feishu_open_id
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
    from src.kernel.session_manager import get_feishu_open_id
    assert get_feishu_open_id("p2puser4") is None
    calls = [c.args[2] for c in api.reply_text.call_args_list if len(c.args) > 2]
    assert not any("GitCode" in t or "确认" in t for t in calls)


def test_already_bound_no_requery():
    """已绑定后不再发确认询问"""
    import asyncio
    from src.kernel.session_manager import set_feishu_open_id
    _seed_user_and_bot("p2puser5", "cli_p2p5", 70005)
    set_feishu_open_id("", "p2puser5", "ou_existing")
    # 补写 app_id（feishu-app-scoped-openid-fix：get_feishu_open_id 按 app_id 过滤）
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("UPDATE unified_user_mapping SET app_id='cli_p2p5' WHERE gitcode_user='p2puser5'")
        db.commit()
    api = asyncio.run(_send({
        "type": "message", "text": "你好", "chat_id": "oc_x",
        "chat_type": "p2p", "sender_open_id": "ou_existing", "message_id": "m1",
    }, app_id="cli_p2p5"))
    calls = [c.args[2] for c in api.reply_text.call_args_list if len(c.args) > 2]
    assert not any("GitCode" in t or "确认" in t for t in calls)
    from src.kernel.session_manager import get_feishu_open_id
    assert get_feishu_open_id("p2puser5") == "ou_existing"


def test_oauth_login_empty_open_id_preserves_binding():
    """OAuth 登录传空 open_id 不得覆盖已有飞书绑定（2026-08-16 修复）。

    回归：此前 auth.py OAuth 登录调 set_feishu_open_id(open_id='') 用 INSERT OR REPLACE
    把 confirmed 的 feishu_open_id 清空，导致系统反复要求重新确认绑定。修复后空 open_id
    应保留已有 open_id，仅刷新昵称。
    """
    import asyncio
    from src.kernel.session_manager import set_feishu_open_id, get_feishu_open_id, db, db_lock
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
    from src.kernel.session_manager import set_feishu_open_id, get_feishu_open_id
    _seed_user_and_bot("p2puser7", "cli_p2p7", 70007)
    set_feishu_open_id("", "p2puser7", "", "", gitcode_nickname="昵称7")
    assert get_feishu_open_id("p2puser7") in (None, ""), "无绑定时空 open_id 不产生 open_id"
