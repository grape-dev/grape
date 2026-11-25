"""Issue #3475 复审修复：新引擎真实命令路径（Router → apply_command →
process_command_async）的渠道透传回归。

engine_wiring.apply_command 原先不传 channel_type，微信等新引擎渠道的 /repo /chat
/task /new 命令提示会退回飞书文案。mgmt「任意拓扑」原语按 import 时的
DEVOPS_DB_PATH 取库，故本文件与 tests/kernel/test_p2p_bind.py 一样在 import 前
把 DEVOPS_DB_PATH 指到共享测试库。
"""
import asyncio
import os

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")

from src.domains.runtime.im.channel.channel import CancelContext, ChannelConfig
from src.domains.runtime.im.channel.message import InboundMessage, Source
from src.domains.runtime.im.channel.types import ChatType, MsgType, Scope, Type
from src.domains.runtime.im.engine_wiring import ImEngine, sync_installation


_APP_ID = "cli_wxcmd1"
_OWNER_UID = "71410"
_SENDER = "wx_sender_cmdpath"


def _clean_rows():
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_t_channel_sender_identity WHERE platform_user_id=?",
                  (_SENDER,))
        c.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id=?",
                  (_APP_ID,))
        db.commit()


def test_wechat_apply_command_passes_channel_type():
    """微信经真实 apply_command 路径下发 /repo：process_command_async 必须收到
    channel_type="wechat"（不透传则未绑定提示硬编码飞书，Issue #3475 复审 Spec-1）。"""
    _clean_rows()
    try:
        assert sync_installation(
            "user", owner_user_id=_OWNER_UID, app_id=_APP_ID, app_secret="sec",
            channel_type="wechat") is True
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT id FROM grape_t_channel_installation "
                            "WHERE legacy_app_id=? AND status='active'",
                            (_APP_ID,)).fetchone()
            inst_id = row[0]
            # 发送者已绑定身份 → Router ⑤ 身份步骤放行，命令才能进入 apply_command
            c.execute("INSERT OR REPLACE INTO grape_t_channel_sender_identity "
                      "(installation_id, platform_user_id, "
                      " platform_stable_user_id, user_id) VALUES (?,?,?,?)",
                      (inst_id, _SENDER, "", _OWNER_UID))
            db.commit()

        recorded = {}

        async def fake_process_command(ctx, egress, text, chat_id, sender_id, db_,
                                       account="", msg_id="", sender_open_id="",
                                       card_value=None, action_token="",
                                       channel_type=""):
            recorded["text"] = text
            recorded["channel_type"] = str(channel_type)
            return ""

        db_path = os.environ["DEVOPS_DB_PATH"]
        engine = ImEngine(db_path, process_command=fake_process_command)
        cfg = ChannelConfig(
            type=Type.WECHAT,
            raw={"app_id": _APP_ID, "app_secret": "sec", "bot_token": "tok"},
            installation_id=inst_id, scope=Scope.USER)
        try:
            assert engine._wechat_factory(cfg) is not None, "微信工厂应成功装配 Router"
            handler = cfg.inbound_handler
            assert handler is not None
            msg = InboundMessage(
                event_id="e_wx_cmd_1", message_id="om_wx_cmd_1",
                source=Source(channel_type=Type.WECHAT, chat_id="wx_chat_cmdpath",
                              chat_type=ChatType.P2P, sender_id=_SENDER),
                type=MsgType.TEXT, text="/repo", command_text="/repo", media_refs=[])
            exc = asyncio.run(handler(CancelContext(), msg))
            assert exc is None, f"router handle failed: {exc!r}"
            assert recorded.get("text") == "/repo"
            assert recorded.get("channel_type") == "wechat", \
                "apply_command 必须透传微信 channel_type，否则命令提示退回飞书文案"
        finally:
            engine._built_apps.clear()
    finally:
        _clean_rows()
