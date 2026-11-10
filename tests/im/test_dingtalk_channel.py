"""钉钉 Channel WS 生命周期测试（Issue #936 P2，§7.6 golden 全链路——ACK 先行 3s）。

替身：fake endpoint_fetcher / fake ws / seen 钩子，不触网。
覆盖：
  - **ACK 先行**：每消息帧先回 code=200（解析失败也 ack），handler 之后才被调
  - SYSTEM 心跳：立即 ack，不分发 handler
  - ACK 3s 时限证据：ack 在 handler 首次记录之前
  - seen 去重（同 msgId 只处理一次）+ 持久化
  - lease 复查失配 → 优雅让渡
  - 链路断开 → ChannelError；凭证永久失败 → PermanentChannelError
  - handler 收到翻译后的 InboundMessage（golden 全链路）
"""
import asyncio
import json

import pytest

from src.im.adapters.dingtalk.channel import DingtalkChannel
from src.im.adapters.dingtalk.outbound import DingtalkSender
from src.im.channel.channel import (
    CancelContext,
    ChannelConfig,
    ChannelError,
    PermanentChannelError,
)
from src.im.channel.types import Scope, Type

# ── golden 帧 ──────────────────────────────────────────

def _msg_frame(msg_id="mid_1", text="hello grape", conv_type="1"):
    return json.dumps({
        "headers": {"contentType": "application/json", "messageId": msg_id,
                    "eventId": f"evt-{msg_id}", "eventType": "chat.message",
                    "appKey": "ding_x"},
        "data": {"conversationId": "cid_100", "msgId": msg_id,
                 "conversationType": conv_type, "senderStaffId": "staff_001",
                 "senderNick": "张三", "msgtype": "text",
                 "text": {"content": text}, "sessionId": "sess_1"},
    }).encode()


def _system_frame():
    return json.dumps({
        "headers": {"contentType": "application/json", "messageId": "sys_1",
                    "eventType": "SYSTEM"},
        "data": {},
    }).encode()


# ── 替身 ──────────────────────────────────────────

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
        self.sent.append(data)

    async def close(self):
        self.closed = True


class FakeConnect:
    def __init__(self, ws):
        self.ws = ws
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.ws


def _channel(frames, handler=None, lease_checker=None, seen=None, endpoint=None):
    seen_store = set() if seen is None else seen
    cfg = ChannelConfig(
        type=Type.DINGTALK,
        raw={"app_id": "ding_app_1", "app_secret": "sec"},
        installation_id="i-dt",
        scope=Scope.USER,
        inbound_handler=handler,
    )
    sender = DingtalkSender(send_text=None, send_markdown=None)
    ws = FakeWS(frames)
    conn = FakeConnect(ws)
    persisted = []

    async def _endpoint(app_id, app_secret):
        if isinstance(endpoint, Exception):
            raise endpoint
        if endpoint is not None:
            return endpoint
        return "wss://api.dingtalk.com/ws", 0

    ch = DingtalkChannel(
        cfg, sender,
        endpoint_fetcher=_endpoint,
        ws_connect=conn,
        lease_checker=lease_checker,
        seen_loader=lambda app: seen_store,
        seen_persist=lambda app, mid: persisted.append(mid),
        seen_cleanup=lambda app: None,
    )
    return ch, ws, conn, persisted


def _parse_ack(raw) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


async def _wait_until(cond, timeout=2.0):
    async def _poll():
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_poll(), timeout=timeout)


# ── 用例 ──────────────────────────────────────────

def test_ack_sent_before_handler_invoked():
    """ACK 先行 3s：ack 帧在 handler 分发之前发出（钉钉时限契约）。"""
    handled = []

    async def handler(ctx, msg):
        handled.append(msg)

    ch, ws, conn, _ = _channel([_msg_frame("m1")], handler=handler)
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    asyncio.run(_wait_until(lambda: bool(handled)))
    acks = [_parse_ack(s) for s in ws.sent if isinstance(s, (bytes, str))]
    assert acks and acks[0]["code"] == 200
    assert acks[0]["headers"]["messageId"] == "m1"
    assert ch.acked_frames == ["m1"]
    assert handled[0].message_id == "m1"


def test_ack_sent_even_when_payload_semantically_empty():
    """可 JSON 解析但无会话字段（解析失败形态）也 ack（防重投死循环）。"""
    bad = json.dumps({
        "headers": {"contentType": "application/json", "messageId": "m_bad",
                    "eventType": "chat.message"},
        "data": {},
    }).encode()
    ch, ws, conn, _ = _channel([bad])
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    acks = [_parse_ack(s) for s in ws.sent]
    assert acks and acks[0]["code"] == 200 and acks[0]["headers"]["messageId"] == "m_bad"
    assert not ch.messages_handled     # 不进 handler，但已 ack


def test_system_frame_acked_not_dispatched():
    """SYSTEM 心跳：立即 ack，不进 handler。"""
    handled = []

    async def handler(ctx, msg):
        handled.append(msg)

    ch, ws, conn, _ = _channel([_system_frame()], handler=handler)
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    acks = [_parse_ack(s) for s in ws.sent]
    assert acks[0]["code"] == 200 and acks[0]["headers"]["messageId"] == "sys_1"
    assert not handled and not ch.acked_frames   # SYSTEM 不算用户消息


def test_seen_dedup_persists_message_id():
    """A3 同构：同 msgId 重投只处理一次 + 持久化。"""
    handled = []

    async def handler(ctx, msg):
        handled.append(msg)

    ch, ws, conn, persisted = _channel(
        [_msg_frame("m1"), _msg_frame("m1"), _msg_frame("m2")], handler=handler)
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    asyncio.run(_wait_until(lambda: len(handled) == 2))
    assert [m.message_id for m in handled] == ["m1", "m2"]
    assert sorted(persisted) == ["m1", "m2"]


def test_handler_receives_translated_inbound_golden():
    """golden 全链路：WS 帧 → ACK → 翻译 → InboundMessage 进 handler。"""
    handled = []

    async def handler(ctx, msg):
        handled.append(msg)

    ch, ws, conn, _ = _channel(
        [_msg_frame("m9", text="@机器人 hi", conv_type="2")], handler=handler)
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    asyncio.run(_wait_until(lambda: bool(handled)))
    m = handled[0]
    assert m.source.channel_type == Type.DINGTALK
    assert m.source.chat_id == "cid_100"
    assert m.source.chat_type.value == "group"
    assert m.source.sender_id == "staff_001"
    assert m.text == "hi" and m.addressed_to_bot is True


def test_lease_lost_graceful_handover():
    ch, ws, conn, _ = _channel([], lease_checker=lambda: False)
    asyncio.run(ch.connect(CancelContext()))    # 正常 return，不连 WS
    assert conn.calls == []


def test_ws_close_raises_for_reconnect():
    ch, ws, conn, _ = _channel([])
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))


def test_permanent_credential_failure_short_circuits():
    """凭证永久失败 → PermanentChannelError（A5：置 revoked 不重试）。"""
    ch, ws, conn, _ = _channel([], endpoint=("", "invalid_app_secret"))
    with pytest.raises(PermanentChannelError):
        asyncio.run(ch.connect(CancelContext()))
    assert conn.calls == []


def test_temporary_endpoint_failure_wrapped():
    ch, ws, conn, _ = _channel([], endpoint=RuntimeError("dns boom"))
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))


def test_cancelled_ctx_returns_cleanly():
    ch, ws, conn, _ = _channel([_msg_frame("m1")])
    ctx = CancelContext()
    ctx.cancel()
    asyncio.run(ch.connect(ctx))
    assert conn.calls


def test_type_and_capabilities():
    ch, ws, _, _ = _channel([])
    assert ch.type() == Type.DINGTALK
    assert ch.capabilities() & 1   # Capability.TEXT 位非零


# ── 审查修正回归（PR #919 note 198166183）──────────────────

def test_default_endpoint_network_error_is_retryable(monkeypatch):
    """取址 HTTP 失败 → ChannelError（可重试，Supervisor 退避重连）。"""
    import httpx

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    from src.im.adapters.dingtalk.channel import _default_endpoint
    with pytest.raises(ChannelError):
        asyncio.run(_default_endpoint("ding_app_1", "sec"))


def test_default_endpoint_builds_ws_url(monkeypatch):
    """open connection 返回 endpoint+ticket → ws 地址；凭证错误码原样透传（不抛）。"""
    import httpx

    class _Resp:
        status_code = 200
        content = b"{}"

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class _Client:
        body = {"endpoint": "wss://x/connect", "ticket": "tk 1"}

        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp(dict(self.body))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    from src.im.adapters.dingtalk.channel import _default_endpoint
    url, err = asyncio.run(_default_endpoint("ding_app_1", "sec"))
    assert err == "" and url.startswith("wss://x/connect?ticket=")

    _Client.body = {"code": "invalidAppKey"}
    url, err = asyncio.run(_default_endpoint("ding_app_1", "sec"))
    assert url == "" and err == "invalidAppKey"


def test_permanent_credentials_error_classification():
    from src.im.adapters.dingtalk.channel import _is_permanent_credentials_error as perm
    assert perm("invalidAppKey") and perm("401") and perm("appNotExist")
    assert not perm("") and not perm("timeout") and not perm("500")


def test_trim_seen_evicts_oldest_in_insertion_order():
    """FIFO 裁剪：按插入序淘汰最旧 msgId（替换 set 迭代序的随机裁剪）。"""
    from src.im.adapters.dingtalk.channel import _trim_seen
    seen = {f"m{i}": None for i in range(10)}
    _trim_seen(seen, 8)          # 保留最新 8//2=4 条
    assert list(seen) == ["m6", "m7", "m8", "m9"]
    _trim_seen(seen, 8)          # 已在限内：无操作
    assert list(seen) == ["m6", "m7", "m8", "m9"]


def _real_shape_frame(msg_id="mid_real"):
    """线上帧形态参考：type=CALLBACK、无 eventType、data 是 JSON 字符串。"""
    data = {
        "senderPlatform": "Win",
        "conversationId": "cid_real_1",
        "msgId": msg_id,
        "senderNick": "张三",
        "senderStaffId": "staff_real",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=s1",
        "conversationType": "1",
        "robotCode": "ding_app_1",
        "msgtype": "text",
        "text": {"content": "你好"},
    }
    return json.dumps({
        "specVersion": "1.0",
        "type": "CALLBACK",
        "headers": {"contentType": "application/json", "messageId": msg_id,
                    "topic": "/v1.0/im/bot/messages/get", "appId": "ding_app_1"},
        "data": json.dumps(data, ensure_ascii=False),
    }).encode()


def test_real_shape_string_data_dispatch_and_webhook_registered():
    """回归：data 为 JSON 字符串时不得 AttributeError 打断 connect，且要登记 sessionWebhook。"""
    from src.im.adapters.dingtalk.outbound import default_webhook_registry
    handled = []

    async def handler(ctx, msg):
        handled.append(msg)

    ch, ws, conn, _ = _channel([_real_shape_frame("mid_real")], handler=handler)
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    asyncio.run(_wait_until(lambda: bool(handled)))
    assert handled and handled[0].message_id == "mid_real"
    assert handled[0].text == "你好"
    assert default_webhook_registry().lookup("cid_real_1")  # webhook 已登记
