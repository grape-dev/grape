"""飞书 WS ack 帧构造测试（原 run_bot 专用用例在旧 IM 下线 M1 时退役）。

ack 帧 payload={"code":200} 的构造仍在用（新引擎 FeishuChannel 每帧 ack 先行）；
该行为的链路级覆盖见 tests/im/test_feishu_channel_lifecycle.py。
"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


def _make_event_frame(message_id="om_test_001", text="你好"):
    """构造一条飞书 im.message.receive_v1 的 DATA(EVENT) 帧字节流。"""
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    from lark_oapi.ws.enum import FrameType, MessageType

    event_data = {
        "schema": "2.0",
        "header": {
            "event_id": "evt_test_001",
            "event_type": "im.message.receive_v1",
            "create_time": "1786000000000",
            "token": "tok",
            "app_id": "cli_test",
            "tenant_key": "t",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_sender", "user_id": "u1"}},
            "message": {
                "message_id": message_id,
                "chat_id": "oc_chat",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }
    frame = Frame()
    frame.method = FrameType.DATA.value
    frame.service = 0
    frame.SeqID = 1
    frame.LogID = 0
    h = frame.headers.add()
    h.key = "type"
    h.value = MessageType.EVENT.value
    h = frame.headers.add()
    h.key = "message_id"
    h.value = message_id
    frame.payload = json.dumps(event_data, ensure_ascii=False).encode("utf-8")
    return frame


def _make_ctx(app_id="cli_acktest"):
    from src.im.feishu_async import BotContext
    ctx = BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="ack-test")
    return ctx


def _frame_from_bytes(data: bytes):
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    f = Frame()
    f.ParseFromString(data)
    return f


async def _run_single_event(frame_bytes, ctx=None, api=None, on_message=None):
    """在 run_bot 的简化环境中消费一条 EVENT 帧，返回 ws.send 的调用记录。"""
    from src.im.feishu_async import _parse_event
    ctx = ctx or _make_ctx()
    api = api or AsyncMock()

    # 简化：直接模拟 run_bot 对单帧的处理（构造 ws 迭代器只产出这一帧）
    ws = AsyncMock()
    # ws 迭代：yield 一帧 bytes 后结束
    frame = _frame_from_bytes(frame_bytes)
    raw_bytes = frame.SerializeToString()
    ws.__aiter__ = lambda: iter([raw_bytes])

    if on_message is None:
        on_message = AsyncMock()

    # 手动复现 run_bot 的 EVENT 处理段（关键逻辑）
    from lark_oapi.ws.enum import FrameType, MessageType
    parsed_frame = _frame_from_bytes(raw_bytes)
    ft = FrameType(parsed_frame.method)
    assert ft == FrameType.DATA
    hs = parsed_frame.headers
    from src.im.adapters.feishu.wire import _get_header, _build_ack_frame, _parse_event as pe
    type_ = _get_header(hs, "type")
    assert type_ == MessageType.EVENT.value
    event_data = json.loads(parsed_frame.payload.decode("utf-8"))
    parsed = pe(event_data)
    # ack 回传
    ack_bytes = _build_ack_frame(parsed_frame)
    return parsed, ack_bytes


def test_ack_frame_payload_is_ok():
    """ack 帧 payload 应为 {"code":200}，且 message_id header 保留。"""
    from src.im.adapters.feishu.wire import _build_ack_frame
    from lark_oapi.ws.enum import FrameType

    frame = _make_event_frame("om_ack_1")
    ack_bytes = _build_ack_frame(frame)
    ack = _frame_from_bytes(ack_bytes)
    # method 保持 DATA
    assert FrameType(ack.method) == FrameType.DATA
    # payload 为 {"code":200}
    assert json.loads(ack.payload.decode("utf-8")) == {"code": 200}
    # headers 保留 message_id
    from src.im.adapters.feishu.wire import _get_header
    assert _get_header(ack.headers, "message_id") == "om_ack_1"
    assert _get_header(ack.headers, "type") == "event"


