"""飞书 Channel WS 生命周期测试（Issue #760 P1——run_bot A6 原样迁入 connect()）。

替身：fake endpoint_fetcher / fake ws / 注入 seen 钩子，不触网。
覆盖（设计文档 §7.5/§7.6 + A5/A3/问题 A）：
  - 永久错误码 10002 → PermanentChannelError（A5 短路）
  - 临时失败 → ChannelError（Supervisor 退避重连依据）
  - 每 DATA 帧 ack 先行（含解析失败也 ack）
  - CONTROL PING → PONG 原位回写
  - seen 去重（同 message_id 只处理一次）+ 持久化调用
  - lease 复查失配 → 优雅让渡 return（问题 A）
  - handler 收到翻译后的 InboundMessage
  - ctx 取消 → 可重入正常返回
"""
import asyncio
import json

import pytest
from lark_oapi.ws.pb.pbbp2_pb2 import Frame
from lark_oapi.ws.enum import FrameType, MessageType

from src.im.channel.channel import (
    CancelContext, ChannelConfig, ChannelError, PermanentChannelError,
)
from src.im.channel.types import Scope, Type
from src.im.adapters.feishu.channel import PERMANENT_WS_ERROR_CODES, FeishuChannel
from src.im.adapters.feishu.outbound import FeishuSender


# ── 帧构造 ──────────────────────────────────────────────

def _data_frame(payload: bytes, header_type=MessageType.EVENT.value) -> bytes:
    f = Frame()
    f.method = FrameType.DATA.value
    f.service = 0
    f.SeqID = 0
    f.LogID = 0
    f.headers.add(key="type", value=header_type)
    f.payload = payload
    return f.SerializeToString()


def _ping_frame() -> bytes:
    f = Frame()
    f.method = FrameType.CONTROL.value
    f.service = 0
    f.SeqID = 0
    f.LogID = 0
    f.headers.add(key="type", value=MessageType.PING.value)
    return f.SerializeToString()


def _parse_sent(raw: bytes) -> Frame:
    f = Frame()
    f.ParseFromString(raw)
    return f


def _msg_event(message_id: str, text: str = "hello", chat_type: str = "p2p") -> bytes:
    return _data_frame(json.dumps({
        "header": {"event_type": "im.message.receive_v1", "event_id": f"evt-{message_id}"},
        "event": {
            "message": {
                "chat_id": "oc1", "chat_type": chat_type, "message_id": message_id,
                "message_type": "text", "content": json.dumps({"text": text}),
            },
            "sender": {"sender_id": {"open_id": "ou1", "user_id": "u1"}},
        },
    }).encode())


# ── 替身 ──────────────────────────────────────────────

class FakeWS:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, data):
        self.sent.append(bytes(data))

    async def close(self):
        self.closed = True


class FakeConnect:
    def __init__(self, ws: FakeWS):
        self.ws = ws
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.ws


def _channel(frames, handler=None, lease_checker=None, seen=None,
             endpoint=None, **kw):
    seen_store = set() if seen is None else seen
    cfg = ChannelConfig(
        type=Type.FEISHU,
        raw={"app_id": "app1", "app_secret": "sec"},
        installation_id="i1",
        scope=Scope.USER,
        inbound_handler=handler,
    )
    sender = FeishuSender(reply_text=None, reply_card=None)
    ws = FakeWS(frames)
    conn = FakeConnect(ws)
    persisted = []

    async def _endpoint(app_id, app_secret):
        if isinstance(endpoint, Exception):
            raise endpoint
        if endpoint is not None:
            return endpoint
        return "wss://example.com/ws?fpid=123&x=1", 0

    ch = FeishuChannel(
        cfg, sender,
        endpoint_fetcher=_endpoint,
        ws_connect=conn,
        lease_checker=lease_checker,
        seen_loader=lambda app: seen_store,
        seen_persist=lambda app, mid: persisted.append(mid),
        seen_cleanup=lambda app: None,
        ping_interval=9999,   # 测试不触发应用层 PING
        **kw,
    )
    return ch, ws, conn, persisted


async def _wait_until(cond, timeout=2.0):
    async def _poll():
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_poll(), timeout=timeout)


# ── 用例 ──────────────────────────────────────────────

async def _test_endpoint_fetch_network_exception_wrapped_impl():
    """契约：endpoint_fetcher 原生网络异常 → 归一为 ChannelError（不绕过 Supervisor 分类）。"""
    ch, ws, conn, _ = _channel([], endpoint=RuntimeError("dns boom"))
    with pytest.raises(ChannelError) as ei:
        await ch.connect(CancelContext())
    assert type(ei.value) is ChannelError   # 非 Permanent，走退避重连
    assert conn.calls == []


async def _test_endpoint_fetch_channel_error_passthrough_impl():
    """fetcher 已抛 ChannelError（含 PermanentChannelError）→ 原样透传不再包装。"""
    ch, ws, conn, _ = _channel([], endpoint=PermanentChannelError("perm"))
    with pytest.raises(PermanentChannelError):
        await ch.connect(CancelContext())


async def _test_permanent_error_code_short_circuits_impl():
    """A5：WS 端点 10002（app 不存在）→ PermanentChannelError，不连 WS。"""
    ch, ws, conn, _ = _channel([], endpoint=("", "10002"))
    with pytest.raises(PermanentChannelError):
        await ch.connect(CancelContext())
    assert conn.calls == []


async def _test_temporary_failure_raises_channel_error_impl():
    """临时失败（非永久码）→ ChannelError（Supervisor 按退避重连）。"""
    ch, ws, conn, _ = _channel([], endpoint=("", "9999"))
    with pytest.raises(ChannelError):
        await ch.connect(CancelContext())


async def _test_ack_sent_for_every_data_frame_impl():
    """ack 先行：可解析的 DATA 帧都回 ack（payload code=200，headers 不变）。"""
    ch, ws, conn, _ = _channel([_msg_event("m1")])
    with pytest.raises(ChannelError):  # 帧耗尽 → 链路断开
        await ch.connect(CancelContext())
    assert len(ws.sent) == 1
    ack = _parse_sent(ws.sent[0])
    assert json.loads(ack.payload.decode()) == {"code": 200}


async def _test_ack_sent_even_when_payload_unparseable_impl():
    """解析失败也 ack（防飞书用新 message_id 反复重投死循环）。"""
    ch, ws, conn, _ = _channel([_data_frame(b"not-json{{")])
    with pytest.raises(ChannelError):
        await ch.connect(CancelContext())
    assert len(ws.sent) == 1  # ack 已回


async def _test_control_ping_gets_pong_impl():
    """飞书应用层 PING → 回 PONG（不回会被判死连接停止推送）。"""
    ch, ws, conn, _ = _channel([_ping_frame()])
    with pytest.raises(ChannelError):
        await ch.connect(CancelContext())
    pong = _parse_sent(ws.sent[0])
    h = next(h for h in pong.headers if h.key == "type")
    assert h.value == MessageType.PONG.value


async def _test_protocol_ping_disabled_impl():
    """协议级 ping 禁用（飞书不回协议级 pong 会超时断连）。"""
    ch, ws, conn, _ = _channel([_msg_event("m1")])
    with pytest.raises(ChannelError):
        await ch.connect(CancelContext())
    assert conn.calls[0][1]["ping_interval"] is None


async def _test_seen_dedup_drops_duplicate_message_id_impl():
    """A3：同 message_id 重投只处理一次（内存 set + 持久化）。"""
    handled = []

    async def handler(ctx, msg):
        handled.append(msg)
        return None

    frames = [_msg_event("m1"), _msg_event("m1"), _msg_event("m2")]
    ch, ws, conn, persisted = _channel(frames, handler=handler)
    with pytest.raises(ChannelError):
        await ch.connect(CancelContext())
    await _wait_until(lambda: len(handled) == 2)
    assert [m.message_id for m in handled] == ["m1", "m2"]
    assert sorted(persisted) == ["m1", "m2"]


async def _test_handler_receives_translated_inbound_impl():
    """翻译链：parsed → InboundMessage 信封进 inbound_handler。"""
    handled = []

    async def handler(ctx, msg):
        handled.append(msg)
        return None

    ch, ws, conn, _ = _channel([_msg_event("m1", text="hi grape")], handler=handler)
    with pytest.raises(ChannelError):
        await ch.connect(CancelContext())
    await _wait_until(lambda: bool(handled))
    m = handled[0]
    assert m.message_id == "m1"
    assert m.text == "hi grape"
    assert m.source.chat_id == "oc1"
    assert m.source.sender_id == "ou1"
    assert m.source.channel_type == Type.FEISHU


async def _test_lease_lost_graceful_handover_impl():
    """问题 A：lease 复查失配 → 优雅让渡（正常 return，不连 WS）。"""
    ch, ws, conn, _ = _channel([], lease_checker=lambda: False)
    await ch.connect(CancelContext())   # 不抛错，不连 WS
    assert conn.calls == []


async def _test_lease_held_proceeds_impl():
    """lease 在手 → 正常连接。"""
    ch, ws, conn, _ = _channel([_msg_event("m1")], lease_checker=lambda: True)
    with pytest.raises(ChannelError):
        await ch.connect(CancelContext())
    assert len(conn.calls) == 1


async def _test_ws_close_raises_for_reconnect_impl():
    """帧耗尽（链路断开）→ ChannelError → Supervisor 退避重连。"""
    ch, ws, conn, _ = _channel([])
    with pytest.raises(ChannelError):
        await ch.connect(CancelContext())


async def _test_cancelled_ctx_returns_cleanly_impl():
    """ctx 取消 → 正常 return（connect 可重入语义）。"""
    ch, ws, conn, _ = _channel([_msg_event("m1")])
    ctx = CancelContext()
    ctx.cancel()
    await ch.connect(ctx)   # 不抛 ChannelError
    assert conn.calls  # 已建立连接但随即退出


async def _test_permanent_codes_match_legacy_impl():
    """永久错误码集合与 legacy 常量一致（A5 继承）。"""
    from src.im.feishu_async import FeishuAsync
    assert set(PERMANENT_WS_ERROR_CODES) == {str(c) for c in FeishuAsync._WS_PERMANENT_ERROR_CODES}


def test_endpoint_fetch_network_exception_wrapped():
    asyncio.run(_test_endpoint_fetch_network_exception_wrapped_impl())

def test_endpoint_fetch_channel_error_passthrough():
    asyncio.run(_test_endpoint_fetch_channel_error_passthrough_impl())

def test_permanent_error_code_short_circuits():
    asyncio.run(_test_permanent_error_code_short_circuits_impl())

def test_temporary_failure_raises_channel_error():
    asyncio.run(_test_temporary_failure_raises_channel_error_impl())

def test_ack_sent_for_every_data_frame():
    asyncio.run(_test_ack_sent_for_every_data_frame_impl())

def test_ack_sent_even_when_payload_unparseable():
    asyncio.run(_test_ack_sent_even_when_payload_unparseable_impl())

def test_control_ping_gets_pong():
    asyncio.run(_test_control_ping_gets_pong_impl())

def test_protocol_ping_disabled():
    asyncio.run(_test_protocol_ping_disabled_impl())

def test_seen_dedup_drops_duplicate_message_id():
    asyncio.run(_test_seen_dedup_drops_duplicate_message_id_impl())

def test_handler_receives_translated_inbound():
    asyncio.run(_test_handler_receives_translated_inbound_impl())

def test_lease_lost_graceful_handover():
    asyncio.run(_test_lease_lost_graceful_handover_impl())

def test_lease_held_proceeds():
    asyncio.run(_test_lease_held_proceeds_impl())

def test_ws_close_raises_for_reconnect():
    asyncio.run(_test_ws_close_raises_for_reconnect_impl())

def test_cancelled_ctx_returns_cleanly():
    asyncio.run(_test_cancelled_ctx_returns_cleanly_impl())

def test_permanent_codes_match_legacy():
    asyncio.run(_test_permanent_codes_match_legacy_impl())
