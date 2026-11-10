"""Slack Channel 生命周期测试（Issue #1309 §8.2）。

替身：假 WS（帧队列 + 发送即记录）+ 假 opener，不触网、不打真 Slack。
覆盖：
  - **ack 必须先于处理**（3s 窗口；用发送顺序 + handler 启动顺序断言）；
  - `hello` 握手、`disconnect` envelope → ChannelError（Supervisor 退避重连）；
  - 坏帧不穿透接收循环（钉钉 #1008 教训）+ `event_id` 重投只处理一次；
  - 凭证缺失 / invalid_auth → PermanentChannelError；网络类 → ChannelError；
  - lease 失配 → 优雅让渡（不建连）；
  - interactive envelope（block_actions）→ 命令消息；
  - 装配：`_slack_factory` 产出 SlackChannel 且能力位齐全。
"""
import asyncio
import json

import pytest

from src.im.adapters.slack.channel import (
    SlackChannel,
    default_connection_opener,
    is_permanent_credentials_error,
)
from src.im.adapters.slack.outbound import SLACK_CAPABILITIES, SlackApiError, SlackClient, SlackSender
from src.im.channel.capability import Capability
from src.im.channel.channel import (
    CancelContext,
    ChannelConfig,
    ChannelError,
    PermanentChannelError,
)
from src.im.channel.registry import ChannelRegistry
from src.im.channel.types import Scope, Type

BOT = "xoxb-test-token"
APP = "xapp-test-token"


class FakeWS:
    """假长连接：send 记录帧；recv 走队列（空则抛错模拟断链）。"""

    def __init__(self, frames=(), on_send=None):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.on_send = on_send
        self.closed = False
        for frame in frames:
            self.queue.put_nowait(frame if isinstance(frame, str) else json.dumps(frame))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        if self.queue.empty():
            raise RuntimeError("ws closed")     # 模拟服务端断链
        return await self.queue.get()

    async def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        if self.on_send is not None:
            self.on_send(frame)

    async def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, url="wss://wss-primary.slack.com/link/?ticket=x", error=None):
        self.url = url
        self.error = error
        self.calls: list = []

    async def __call__(self, app_token):
        self.calls.append(app_token)
        if self.error is not None:
            raise self.error
        return self.url


def _envelope(event=None, event_id="Ev1", envelope_id="e-1", etype="events_api"):
    payload = {
        "type": "event_callback",
        "event_id": event_id,
        "event": event or {
            "type": "app_mention", "channel": "C1", "user": "U1",
            "text": "<@U0BOT> hi", "ts": "1.1",
        },
    }
    return {"envelope_id": envelope_id, "type": etype,
            "payload": json.dumps(payload, ensure_ascii=False)}


def _channel(handler=None, **kw):
    cfg = ChannelConfig(type=Type.SLACK,
                        raw={"bot_token": BOT, "app_token": APP},
                        installation_id="inst-slack", scope=Scope.USER,
                        inbound_handler=handler)
    sender = SlackSender(SlackClient(BOT), min_interval=0.0)
    return SlackChannel(cfg, sender, **kw)


def _recorder(calls):
    async def handler(ctx, msg):
        calls.append(msg)
        return None

    return handler


# ── ack 先行 ────────────────────────────────────────────────

async def test_ack_precedes_handler():
    calls: list = []
    order: list = []
    ch = _channel(_recorder(calls))
    ws = FakeWS(on_send=lambda frame: order.append("ack:" + frame["envelope_id"]))

    async def handler(ctx, msg):
        order.append("handle:" + (msg.text or ""))
        calls.append(msg)

    ch._cfg.inbound_handler = handler
    await ch._on_frame(CancelContext(), ws, json.dumps(_envelope()), {})
    await asyncio.sleep(0.02)
    assert order == ["ack:e-1", "handle:hi"], order
    assert ch.acked_frames == ["e-1"]
    assert len(ch.messages_handled) == 1


async def test_duplicate_event_id_dispatched_once_but_acked_twice():
    calls: list = []
    ch = _channel(_recorder(calls))
    ws = FakeWS()
    seen: dict = {}
    await ch._on_frame(CancelContext(), ws, json.dumps(_envelope(envelope_id="e-a")), seen)
    # Slack 重投：新 envelope_id，同 event_id
    await ch._on_frame(CancelContext(), ws, json.dumps(_envelope(envelope_id="e-b")), seen)
    await asyncio.sleep(0.02)
    assert [f["envelope_id"] for f in ws.sent] == ["e-a", "e-b"]   # 两次都 ack
    assert len(calls) == 1                                        # 只处理一次
    assert len(seen) == 1


async def test_hello_frame_marks_connected_without_ack():
    ch = _channel()
    ws = FakeWS()
    await ch._on_frame(CancelContext(), ws, json.dumps({"type": "hello", "num_connections": 1}), {})
    assert ch.hello is True
    assert ws.sent == []


async def test_disconnect_envelope_raises_channel_error():
    ch = _channel()
    ws = FakeWS()
    with pytest.raises(ChannelError):
        await ch._on_frame(CancelContext(), ws, json.dumps({"type": "disconnect", "reason": "refresh"}), {})


async def test_bad_frame_does_not_break_receive_loop():
    calls: list = []
    ch = _channel(_recorder(calls))
    ws = FakeWS()
    await ch._on_frame(CancelContext(), ws, "{not json", {})
    assert ch.parse_fail == 1
    await ch._on_frame(CancelContext(), ws, json.dumps(_envelope()), {})
    await asyncio.sleep(0.02)
    assert len(calls) == 1


async def test_interactive_envelope_becomes_command():
    calls: list = []
    ch = _channel(_recorder(calls))
    ws = FakeWS()
    payload = {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"},
               "message": {"ts": "1.9"},
               "actions": [{"action_id": "bind", "value": json.dumps({"cmd": "/bind 2"})}]}
    env = {"envelope_id": "e-card", "type": "interactive",
           "payload": json.dumps(payload, ensure_ascii=False)}
    await ch._on_frame(CancelContext(), ws, json.dumps(env), {})
    await asyncio.sleep(0.02)
    assert ws.sent[0]["envelope_id"] == "e-card"
    assert calls[0].text == "/bind 2"


async def test_unknown_envelope_type_is_acked_and_ignored():
    ch = _channel()
    ws = FakeWS()
    env = {"envelope_id": "e-slash", "type": "slash_commands",
           "payload": json.dumps({"command": "/grape"})}
    await ch._on_frame(CancelContext(), ws, json.dumps(env), {})
    await asyncio.sleep(0.01)
    assert ws.sent == [{"envelope_id": "e-slash"}]
    assert ch.messages_handled == []


# ── connect 生命周期 ────────────────────────────────────────

async def test_connect_hello_then_closed_raises_channel_error():
    opener = FakeOpener()
    ws = FakeWS(frames=[{"type": "hello", "num_connections": 1}, _envelope()])
    ch = _channel(_recorder([]), connection_opener=opener,
                  ws_connect=lambda url: ws)
    with pytest.raises(ChannelError):
        await ch.connect(CancelContext())
    assert opener.calls == [APP]
    assert ch.hello is True
    assert ch.acked_frames == ["e-1"]


async def test_connect_missing_tokens_is_permanent():
    cfg = ChannelConfig(type=Type.SLACK, raw={}, installation_id="i1")
    ch = SlackChannel(cfg, SlackSender(SlackClient(""), min_interval=0))
    with pytest.raises(PermanentChannelError):
        await ch.connect(CancelContext())


async def test_invalid_auth_is_permanent():
    ch = _channel(connection_opener=FakeOpener(
        error=SlackApiError("apps.connections.open failed: invalid_auth")))
    with pytest.raises(PermanentChannelError):
        await ch.connect(CancelContext())


async def test_transient_open_failure_is_retryable():
    ch = _channel(connection_opener=FakeOpener(error=RuntimeError("network down")))
    with pytest.raises(ChannelError) as exc:
        await ch.connect(CancelContext())
    assert not isinstance(exc.value, PermanentChannelError)


async def test_lease_lost_returns_without_opening():
    opener = FakeOpener()
    ch = _channel(connection_opener=opener, lease_checker=lambda: False)
    assert await ch.connect(CancelContext()) is None
    assert opener.calls == []


def test_permanent_error_classification():
    assert is_permanent_credentials_error("apps.connections.open failed: invalid_auth")
    assert is_permanent_credentials_error("missing_scope")
    assert not is_permanent_credentials_error("rate limited")


async def test_disconnect_closes_socket():
    """优雅让渡/停机上必须主动关连接（防 recv 阻塞留僵尸 socket）。"""
    ch = _channel()
    ws = FakeWS()
    ch._ws = ws
    await ch.disconnect()
    assert ws.closed is True
    assert ch._ws is None


# ── 装配 ────────────────────────────────────────────────────

def test_slack_factory_builds_channel_with_full_capabilities(tmp_path):
    from src.im.engine_wiring import ImEngine

    engine = ImEngine(str(tmp_path / "im.db"), registry=ChannelRegistry(),
                      lease_store=object(), scan_interval=3600)
    cfg = ChannelConfig(type=Type.SLACK,
                        raw={"app_id": BOT, "app_secret": APP},
                        installation_id="inst-slack", scope=Scope.USER)
    channel = engine._slack_factory(cfg)
    assert isinstance(channel, SlackChannel)
    caps = channel.capabilities()
    for bit in (Capability.TEXT, Capability.RICH_CARD, Capability.CARD_ACTION,
                Capability.THREAD_REPLY, Capability.MESSAGE_EDIT,
                Capability.ATTACHMENT, Capability.REACTION):
        assert caps & bit, bit
    # 归一后的 config 键（双令牌语义 + app_id 兼容键）
    assert cfg.raw["bot_token"] == BOT and cfg.raw["app_token"] == APP
    assert cfg.raw["app_id"] == BOT and cfg.raw["app_secret"] == APP
    assert SLACK_CAPABILITIES == caps


def test_default_connection_opener_shape():
    """默认 opener 是 async 可调用（真实网络路径由集成环境验证，这里只校验签名）。"""
    assert asyncio.iscoroutinefunction(default_connection_opener)
