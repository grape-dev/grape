"""企微 Channel 生命周期测试（Issue #1254 批 1）。

替身：假 WS（帧队列，发送即自动回 ack）+ 假媒体下载器，不触网、不打真企微。
覆盖：
  - 订阅先行且**只发一次**（平台有订阅频率保护），10s 内 ack 才进入收帧；
  - 凭证错误（errcode=40001）→ PermanentChannelError（Supervisor 置 revoked）；
  - ack 超时 → ChannelError（可重连，不静默假活）；
  - 30s ping 保活（测试注入 10ms）；
  - 90s 读超时 → ChannelError；
  - disconnected_event（被新连接踢掉）→ ChannelError；
  - 坏帧不穿透接收循环（钉钉 #1008 教训）+ seen 去重；
  - 收帧即下载媒体（5 分钟 URL 的有效期约束）+ AES-256-CBC 解密；
  - lease 失配 → 优雅让渡（不建连）。
"""
import asyncio
import base64
import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src.im.adapters.wecom.channel import WecomChannel
from src.im.adapters.wecom.outbound import WecomReplyRegistry, WecomSender, WecomTransport
from src.im.adapters.wecom.resolvers import WecomMediaResolver
from src.im.channel.channel import (
    CancelContext,
    ChannelConfig,
    ChannelError,
    PermanentChannelError,
)
from src.im.channel.types import ChatType, Scope, MsgType, Type


# ── 帧工具 ──────────────────────────────────────────

def _msg_frame(msgid="mid_1", chattype="single", chatid="", text="你好",
               msgtype="text", extra=None):
    body = {"msgid": msgid, "aibotid": "bot_x", "chattype": chattype,
            "from": {"userid": "zhangsan"}, "msgtype": msgtype}
    if chatid:
        body["chatid"] = chatid
    if msgtype == "text":
        body["text"] = {"content": text}
    body.update(extra or {})
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": f"req-{msgid}"},
            "body": json.dumps(body, ensure_ascii=False)}


def _disconnect_frame():
    return {"cmd": "aibot_event_callback", "headers": {"req_id": "req-dc"},
            "body": json.dumps({"msgid": "evt_dc", "msgtype": "event",
                                "event": {"eventtype": "disconnected_event"}})}


def _ack(req_id, errcode=0):
    return {"headers": {"req_id": req_id}, "errcode": errcode, "errmsg": "ok"}


def _encrypt(plain: bytes, aeskey_b64: str) -> bytes:
    key = base64.b64decode(aeskey_b64.replace("-", "+").replace("_", "/") + "==")
    key = key[:32]
    pad = 32 - (len(plain) % 32)
    padded = plain + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return enc.update(padded) + enc.finalize()


# ── 假 WS ──────────────────────────────────────────

class FakeWS:
    """帧队列假长连接：send 自动回 ack（可按 cmd 定制），recv 走队列。"""

    def __init__(self, seed=(), subscribe_errcode=0, auto_ack=True):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.subscribe_errcode = subscribe_errcode
        self.auto_ack = auto_ack
        self.closed = False
        for frame in seed:
            self.queue.put_nowait(json.dumps(frame, ensure_ascii=False))
        self.recv_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        self.recv_calls += 1
        return await self.queue.get()

    async def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        if not self.auto_ack:
            return
        req_id = (frame.get("headers") or {}).get("req_id", "")
        cmd = frame.get("cmd")
        if cmd == "aibot_subscribe":
            self.queue.put_nowait(json.dumps(
                _ack(req_id, self.subscribe_errcode), ensure_ascii=False))
        elif cmd in ("ping", "pong"):
            self.queue.put_nowait(json.dumps(_ack(req_id), ensure_ascii=False))
        else:
            self.queue.put_nowait(json.dumps(_ack(req_id), ensure_ascii=False))

    async def close(self):
        self.closed = True


class FakeConnect:
    def __init__(self, ws):
        self._ws = ws
        self.urls: list = []

    def __call__(self, url):
        self.urls.append(url)
        return self._ws


class FakeResolver(WecomMediaResolver):
    """下载器替身：按 url 返回预置密文，不触网。"""

    def __init__(self, blobs=None):
        super().__init__(http_get=self._get)
        self._blobs = dict(blobs or {})

    async def _get(self, url):
        return self._blobs[url]


class Recorder:
    def __init__(self):
        self.messages: list = []

    async def __call__(self, ctx, msg):
        self.messages.append(msg)
        return None


def _cfg(raw=None, handler=None, scope=Scope.USER):
    base = {"app_id": "bot_x", "app_secret": "sec_x"}
    base.update(raw or {})
    return ChannelConfig(type=Type.WECOM, raw=base, scope=scope,
                         inbound_handler=handler)


def _channel(ws, handler, resolver=None, cfg_raw=None, lease=None):
    cfg = _cfg(cfg_raw, handler)
    transport = WecomTransport("bot_x")
    registry = WecomReplyRegistry()
    media = resolver or WecomMediaResolver()
    ch = WecomChannel(cfg, WecomSender(transport, registry), transport=transport,
                      reply_registry=registry, media_resolver=media,
                      ws_connect=FakeConnect(ws), lease_checker=lease)
    return ch, transport, registry


async def _drain(seconds=0.15):
    await asyncio.sleep(seconds)


# ── 测试 ──────────────────────────────────────────

def test_golden_message_flow_dispatches_translated_inbound():
    async def run():
        ws = FakeWS([_msg_frame(text="你好 grape")])
        rec = Recorder()
        ch, transport, registry = _channel(ws, rec)
        ctx = CancelContext()
        task = asyncio.create_task(ch.connect(ctx))
        await _drain()
        ctx.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (ChannelError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return ws, rec, ch, registry

    ws, rec, ch, registry = asyncio.run(run())
    assert ch.subscribed is True
    assert len(rec.messages) == 1
    msg = rec.messages[0]
    assert msg.source.channel_type is Type.WECOM
    assert msg.source.chat_type is ChatType.P2P
    assert msg.source.chat_id == "zhangsan"
    assert msg.text == "你好 grape"
    # 订阅帧只发一次且带 bot_id/secret
    subs = [f for f in ws.sent if f.get("cmd") == "aibot_subscribe"]
    assert len(subs) == 1
    assert subs[0]["body"] == {"bot_id": "bot_x", "secret": "sec_x"}
    # 回复令牌登记（出站按 chat_id 取 req_id）
    assert registry.reply_req_id("zhangsan") == "req-mid_1"


def test_subscribe_rejected_with_bad_credentials_is_permanent():
    async def run():
        ws = FakeWS([], subscribe_errcode=40001)
        ch, _, _ = _channel(ws, Recorder())
        with pytest.raises(PermanentChannelError):
            await ch.connect(CancelContext())

    asyncio.run(run())


def test_subscribe_ack_timeout_is_retryable_error():
    async def run():
        ws = FakeWS([], auto_ack=False)
        ch, _, _ = _channel(ws, Recorder(), cfg_raw={"subscribe_timeout": 0.05})
        with pytest.raises(ChannelError) as exc:
            await ch.connect(CancelContext())
        assert not isinstance(exc.value, PermanentChannelError)

    asyncio.run(run())


def test_ping_keepalive_is_sent_periodically():
    async def run():
        ws = FakeWS([_msg_frame()])
        ch, _, _ = _channel(ws, Recorder(), cfg_raw={"ping_interval": 0.02})
        ctx = CancelContext()
        task = asyncio.create_task(ch.connect(ctx))
        await _drain(0.2)
        ctx.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (ChannelError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return ws

    ws = asyncio.run(run())
    assert any(f.get("cmd") == "ping" for f in ws.sent)


def test_read_timeout_raises_channel_error():
    async def run():
        ws = FakeWS([])                      # 队列空 → recv 永久阻塞
        ch, _, _ = _channel(ws, Recorder(), cfg_raw={"read_timeout": 0.05})
        with pytest.raises(ChannelError) as exc:
            await ch.connect(CancelContext())
        assert "read timeout" in str(exc.value)

    asyncio.run(run())


def test_disconnected_event_raises_channel_error():
    async def run():
        ws = FakeWS([_disconnect_frame()])
        ch, _, _ = _channel(ws, Recorder())
        with pytest.raises(ChannelError) as exc:
            await ch.connect(CancelContext())
        assert "kicked" in str(exc.value)

    asyncio.run(run())


def test_bad_frame_does_not_kill_receive_loop():
    async def run():
        ws = FakeWS([], )
        ws.queue.put_nowait("not-a-json")
        ws.queue.put_nowait(json.dumps(_msg_frame(msgid="mid_ok")))
        rec = Recorder()
        ch, _, _ = _channel(ws, rec)
        ctx = CancelContext()
        task = asyncio.create_task(ch.connect(ctx))
        await _drain()
        ctx.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (ChannelError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return rec, ch

    rec, ch = asyncio.run(run())
    assert ch.parse_fail >= 1
    assert len(rec.messages) == 1          # 坏帧之后仍能收到下一条


def test_duplicate_msgid_is_deduplicated():
    async def run():
        ws = FakeWS([_msg_frame(msgid="mid_dup"), _msg_frame(msgid="mid_dup")])
        rec = Recorder()
        ch, _, _ = _channel(ws, rec)
        ctx = CancelContext()
        task = asyncio.create_task(ch.connect(ctx))
        await _drain()
        ctx.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (ChannelError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return rec

    rec = asyncio.run(run())
    assert len(rec.messages) == 1


def test_media_is_downloaded_and_decrypted_at_frame_time():
    plain = b"\x89PNG\r\n\x1a\n fake png body"
    aeskey = base64.b64encode(os.urandom(32)).decode()
    url = "https://cos.example/x.png"
    cipher = _encrypt(plain, aeskey)
    frame = _msg_frame(msgtype="image",
                       extra={"image": {"url": url, "aeskey": aeskey}})

    async def run():
        ws = FakeWS([frame])
        rec = Recorder()
        resolver = FakeResolver({url: cipher})
        ch, _, _ = _channel(ws, rec, resolver=resolver)
        ctx = CancelContext()
        task = asyncio.create_task(ch.connect(ctx))
        await _drain()
        ctx.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (ChannelError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return rec, resolver, ch

    rec, resolver, ch = asyncio.run(run())
    msg = rec.messages[0]
    assert msg.type is MsgType.IMAGE
    key = msg.media_refs[0].storage_key
    assert key.startswith("wecom:")
    assert resolver.cache.get(key) == plain       # 收帧即解密落缓存
    assert ch.media_cache.get(key) == plain


def test_lease_mismatch_returns_without_connecting():
    async def run():
        ws = FakeWS([_msg_frame()])
        ch, _, _ = _channel(ws, Recorder(), lease=lambda: False)
        await ch.connect(CancelContext())
        return ws

    ws = asyncio.run(run())
    assert ws.sent == []


def test_missing_credentials_is_permanent_error():
    async def run():
        ws = FakeWS([])
        cfg = _cfg({"app_id": "", "app_secret": ""}, Recorder())
        transport = WecomTransport("")
        ch = WecomChannel(cfg, WecomSender(transport), transport=transport,
                          ws_connect=FakeConnect(ws))
        with pytest.raises(PermanentChannelError):
            await ch.connect(CancelContext())

    asyncio.run(run())


def test_group_message_routes_by_chatid():
    async def run():
        ws = FakeWS([_msg_frame(chattype="group", chatid="chat_9", text="@bot 帮我看看")])
        rec = Recorder()
        ch, _, registry = _channel(ws, rec)
        ctx = CancelContext()
        task = asyncio.create_task(ch.connect(ctx))
        await _drain()
        ctx.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (ChannelError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return rec, registry

    rec, registry = asyncio.run(run())
    msg = rec.messages[0]
    assert msg.source.chat_id == "chat_9"
    assert msg.text == "帮我看看"
    assert registry.chat_type("chat_9") == 2       # 主动推送需要 chat_type=2


def test_group_at_other_user_is_not_addressed_when_bot_name_configured():
    async def run():
        ws = FakeWS([_msg_frame(chattype="group", chatid="chat_9", text="@Alice 你好")])
        rec = Recorder()
        ch, _, _ = _channel(ws, rec, cfg_raw={"bot_name": "RobotA"})
        ctx = CancelContext()
        task = asyncio.create_task(ch.connect(ctx))
        await _drain()
        ctx.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (ChannelError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return rec

    rec = asyncio.run(run())
    assert rec.messages[0].addressed_to_bot is False


def test_capabilities_come_from_sender():
    ws = FakeWS([])
    ch, _, _ = _channel(ws, Recorder())
    from src.im.adapters.wecom.outbound import WECOM_CAPABILITIES
    assert ch.capabilities() == WECOM_CAPABILITIES
    assert ch.type() is Type.WECOM

# ── 装配注册（engine_wiring，约 40 行的改动不能被静默跳过）──────────────

def test_wiring_registers_wecom_renderer_and_validator():
    from src.im.channel.card import BUILTIN_CARD_RENDERERS, self_check_card_capabilities
    from src.im.channel.renderers.wecom import WecomCardRenderer
    from src.im.engine_wiring import _card_renderer_for, _msg_id_validator_for
    from src.im.adapters.wecom.outbound import WECOM_CAPABILITIES

    assert isinstance(_card_renderer_for("wecom"), WecomCardRenderer)
    # 企微按 chatid/userid 路由回复，msg_id 形态无关 → 恒可用
    assert _msg_id_validator_for("wecom")("any-route") is True
    assert "wecom" in BUILTIN_CARD_RENDERERS
    # 声明 RICH_CARD 必须有 renderer（启动自检不得报警）
    assert self_check_card_capabilities({"wecom": WECOM_CAPABILITIES}) == []


def test_wecom_factory_goes_through_generic_channel_factory():
    """工厂必须复用 _channel_factory（同一套 Router 装配 + 去重），只换实现。"""
    from src.im.engine_wiring import ImEngine

    engine = ImEngine.__new__(ImEngine)      # 跳过构造（DB 装配与本用例无关）
    seen: dict = {}

    def _fake_channel_factory(cfg, channel_type, sender_cls, build_channel,
                              sender_factory=None):
        seen.update(channel_type=channel_type, sender_factory=sender_factory,
                    build_channel=build_channel, cfg=cfg)
        return "channel-stub"

    engine._channel_factory = _fake_channel_factory
    cfg = ChannelConfig(type=Type.WECOM, raw={"app_id": "bot_1", "app_secret": "sec"})
    assert engine._wecom_factory(cfg) == "channel-stub"
    assert seen["channel_type"] == "wecom"
    assert callable(seen["sender_factory"])
    channel = seen["build_channel"](cfg, "sender-stub")
    assert isinstance(channel, WecomChannel)
