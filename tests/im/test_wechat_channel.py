"""微信 Channel 生命周期测试（Issue #1728 M1）。

替身：假 update_fetcher + 假 sender，不触网。覆盖：
  - getupdates 游标变化 → 持久化，重启从断点续接；
  - `ret=-14` → 暂停 1h（可注入短暂停）+ 计 `session_paused_risk`，不抛；
  - 自消息与空消息丢弃（inbound_empty 计数）；
  - 重复消息只处理一次；坏帧不穿透（parse_fail 计数）；
  - 凭证缺失 → PermanentChannelError；lease 失配 → 优雅让渡；
  - notifystart/notifystop 生命周期。
"""
import asyncio

import pytest

from src.im.adapters.wechat.channel import WechatChannel
from src.im.adapters.wechat.protocol import WechatApiError
from src.im.channel.channel import (
    CancelContext,
    ChannelConfig,
    ChannelError,
    PermanentChannelError,
)
from src.im.channel.message import SendResult
from src.im.channel.types import Type


class FakeSender:
    def __init__(self):
        self.sent = []
        self.started = 0
        self.stopped = 0
        self.typing = []

    def capabilities(self):
        from src.im.adapters.wechat.outbound import WECHAT_CAPABILITIES
        return WECHAT_CAPABILITIES

    async def send(self, out):
        self.sent.append(out)
        return SendResult(ok=True)

    async def send_typing(self, chat_id, status=1):
        self.typing.append((chat_id, status))
        return True

    async def notify_start(self):
        self.started += 1

    async def notify_stop(self):
        self.stopped += 1


class FakeMetrics:
    def __init__(self):
        self.reasons = []

    def record_degraded(self, reason=""):
        self.reasons.append(reason)


class StopLoop(Exception):
    pass


def _cfg(handler=None, token="bt-1"):
    return ChannelConfig(type=Type.WECHAT, raw={"bot_token": token, "app_id": "ib-1"},
                         inbound_handler=handler)


def _frame(message_id=7001, text="hello", **overrides):
    msg = {
        "seq": message_id,
        "message_id": message_id,
        "from_user_id": "alice@im.wechat",
        "to_user_id": "bot@im.wechat",
        "message_type": 1,
        "context_token": "ctx-1",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    msg.update(overrides)
    return msg


def _response(msgs, cursor="c-2"):
    return {"msgs": msgs, "get_updates_buf": cursor}


def test_cursor_advances_and_persists():
    persisted = []

    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                return _response([_frame()], cursor="c-2")
            raise StopLoop()

    ch = WechatChannel(_cfg(), FakeSender(), update_fetcher=Fetcher(),
                       cursor_loader=lambda: "c-1",
                       cursor_persist=persisted.append)
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    assert ch.cursor == "c-2"
    assert persisted == ["c-2"]


def test_unchanged_cursor_not_repersisted():
    persisted = []

    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                return _response([_frame()], cursor="same")
            raise StopLoop()

    ch = WechatChannel(_cfg(), FakeSender(), update_fetcher=Fetcher(),
                       cursor_loader=lambda: "same",
                       cursor_persist=persisted.append)
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    assert persisted == []


def test_ret_minus_14_pauses_and_records_degraded_without_channel_error():
    metrics = FakeMetrics()

    class Fetcher:
        async def __call__(self, cursor, timeout):
            raise WechatApiError("paused", ret=-14)

    sender = FakeSender()
    ch = WechatChannel(_cfg(), sender, update_fetcher=Fetcher(), metrics=metrics,
                       pause_seconds=0.01)
    asyncio.run(ch.connect(CancelContext()))
    assert ch.session_paused == 1
    assert "session_paused_risk" in metrics.reasons
    assert ch.connected is False
    assert sender.stopped >= 1


def test_message_dispatched_to_handler():
    handled = []

    async def handler(ctx, msg):
        handled.append(msg)
        return None

    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                return _response([_frame(7001, "hi")])
            raise StopLoop()

    ch = WechatChannel(_cfg(handler), FakeSender(), update_fetcher=Fetcher())

    async def run():
        with pytest.raises(ChannelError):
            await ch.connect(CancelContext())
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert [m.text for m in handled] == ["hi"]


def test_bot_and_empty_messages_dropped():
    metrics = FakeMetrics()

    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                return _response([
                    _frame(1, message_type=2),
                    {"message_id": 2, "from_user_id": "u", "message_type": 1, "item_list": []},
                ])
            raise StopLoop()

    ch = WechatChannel(_cfg(), FakeSender(), update_fetcher=Fetcher(),
                       metrics=metrics)
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    assert ch.inbound_empty == 2
    assert metrics.reasons.count("inbound_empty") == 2
    assert ch.messages_handled == []


def test_duplicate_message_handled_once():
    handled = []

    async def handler(ctx, msg):
        handled.append(msg.message_id)

    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                return _response([_frame(7001), _frame(7001)], cursor="a")
            if self.calls == 2:
                return _response([_frame(7001)], cursor="b")
            raise StopLoop()

    ch = WechatChannel(_cfg(handler), FakeSender(), update_fetcher=Fetcher())

    async def run():
        with pytest.raises(ChannelError):
            await ch.connect(CancelContext())
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert handled == ["7001"]


def test_missing_token_raises_permanent():
    ch = WechatChannel(_cfg(token=""), FakeSender())
    with pytest.raises(PermanentChannelError):
        asyncio.run(ch.connect(CancelContext()))


def test_lease_mismatch_yields_gracefully():
    ch = WechatChannel(_cfg(), FakeSender(), lease_checker=lambda: False)
    asyncio.run(ch.connect(CancelContext()))
    assert ch.connected is False


def test_notify_start_stop_lifecycle():
    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            raise StopLoop()

    sender = FakeSender()
    ch = WechatChannel(_cfg(), sender, update_fetcher=Fetcher())
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    assert sender.started == 1
    assert sender.stopped >= 1


def test_bad_frame_does_not_break_loop():
    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                return _response(["not-a-dict", _frame(7002, "ok")])
            raise StopLoop()

    ch = WechatChannel(_cfg(), FakeSender(), update_fetcher=Fetcher())
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    assert ch.parse_fail == 0  # 坏帧被容错为 None → inbound_empty，不穿透
    assert ch.inbound_empty == 1


def test_typing_sent_on_inbound():
    sender = FakeSender()

    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                return _response([_frame(7003)])
            raise StopLoop()

    ch = WechatChannel(_cfg(), sender, update_fetcher=Fetcher())

    async def run():
        with pytest.raises(ChannelError):
            await ch.connect(CancelContext())
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert sender.typing and sender.typing[0][0] == "alice@im.wechat"