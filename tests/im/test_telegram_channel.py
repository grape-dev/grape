"""Telegram Channel 生命周期测试（Issue #1613 批 1/3/4）。

替身：假 update_fetcher + 假 sender，不触网。覆盖：
  - getUpdates offset 推进并持久化（重启从 last+1 继续，不重投）；
  - `edited_message` 忽略但 offset 仍推进；
  - `callback_query` **先 answerCallbackQuery 再派发**；
  - 重复 update 只处理一次；坏 update 不穿透（parse_fail 计数）；
  - 凭证缺失 → PermanentChannelError；401 → PermanentChannelError；
  - lease 失配 → 优雅让渡（不建连）。
"""
import asyncio

import pytest

from src.im.adapters.telegram.channel import TelegramChannel
from src.im.adapters.telegram.outbound import TELEGRAM_CAPABILITIES, TelegramApiError
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

    def capabilities(self):
        return TELEGRAM_CAPABILITIES

    async def send(self, out):
        self.sent.append(out)
        return SendResult(ok=True)


class StopLoop(Exception):
    pass


def _cfg(handler=None, token="123:abc"):
    return ChannelConfig(type=Type.TELEGRAM, raw={"bot_token": token},
                         inbound_handler=handler)


def _message_update(update_id=100, text="hello"):
    return {"update_id": update_id, "message": {
        "message_id": update_id, "chat": {"id": 123, "type": "private"},
        "from": {"id": 42, "first_name": "A"}, "text": text}}


def _callback_update(update_id=101, data="b:2"):
    return {"update_id": update_id, "callback_query": {
        "id": f"cq-{update_id}", "from": {"id": 42}, "data": data,
        "message": {"message_id": 7, "chat": {"id": 123, "type": "private"}}}}


def test_offset_advances_and_persists():
    persisted = []

    class Fetcher:
        def __init__(self):
            self.calls = []

        async def __call__(self, offset, timeout):
            self.calls.append(offset)
            if len(self.calls) == 1:
                return [_message_update(100), _message_update(101)]
            raise StopLoop()

    fetcher = Fetcher()
    ch = TelegramChannel(_cfg(), FakeSender(), update_fetcher=fetcher,
                         offset_loader=lambda: 100,
                         offset_persist=persisted.append,
                         bot_username="grape_bot")

    async def run():
        with pytest.raises(ChannelError):
            await ch.connect(CancelContext())

    asyncio.run(run())
    assert fetcher.calls[0] == 100
    assert persisted == [101, 102]
    assert ch.offset == 102


def test_edited_message_advances_offset_without_dispatch():
    persisted = []
    handled = []

    async def handler(ctx, msg):
        handled.append(msg)

    class Fetcher:
        def __init__(self):
            self.calls = []

        async def __call__(self, offset, timeout):
            self.calls.append(offset)
            if len(self.calls) == 1:
                return [{"update_id": 200,
                         "edited_message": {"message_id": 1}}]
            raise StopLoop()

    ch = TelegramChannel(_cfg(handler), FakeSender(), update_fetcher=Fetcher(),
                         offset_persist=persisted.append, bot_username="grape_bot")

    async def run():
        with pytest.raises(ChannelError):
            await ch.connect(CancelContext())
        await asyncio.sleep(0)

    asyncio.run(run())
    assert persisted == [201]
    assert handled == []


def test_callback_ack_happens_before_dispatch():
    order = []

    async def handler(ctx, msg):
        order.append("handled")

    async def answer(cq_id):
        order.append("answered")

    ch = TelegramChannel(_cfg(handler), FakeSender(),
                         update_fetcher=None, answer_callback=answer,
                         bot_username="grape_bot")
    ctx = CancelContext()

    async def run():
        await ch._on_update(ctx, _callback_update())
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert order == ["answered", "handled"]
    assert ch.answered_callbacks == ["cq-101"]


def test_duplicate_update_handled_once():
    handled = []

    async def handler(ctx, msg):
        handled.append(msg.event_id)

    ch = TelegramChannel(_cfg(handler), FakeSender(), bot_username="grape_bot")
    ctx = CancelContext()
    update = _message_update(300)

    async def run():
        await ch._on_update(ctx, update)
        await ch._on_update(ctx, update)
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert handled == ["300"]


def test_bad_update_does_not_break_loop():
    ch = TelegramChannel(_cfg(), FakeSender(), bot_username="grape_bot")
    asyncio.run(ch._on_update(CancelContext(), "not-a-dict"))
    assert ch.parse_fail == 1


def test_missing_token_permanent_error():
    ch = TelegramChannel(_cfg(token=""), FakeSender())
    with pytest.raises(PermanentChannelError):
        asyncio.run(ch.connect(CancelContext()))


def test_401_permanent_error_through_connect():
    class Fetcher:
        async def __call__(self, offset, timeout):
            raise TelegramApiError("Unauthorized", code=401)

    ch = TelegramChannel(_cfg(), FakeSender(), update_fetcher=Fetcher())
    with pytest.raises(PermanentChannelError):
        asyncio.run(ch.connect(CancelContext()))


def test_lease_lost_returns_without_connecting():
    class Fetcher:
        async def __call__(self, offset, timeout):
            raise AssertionError("should not fetch")

    ch = TelegramChannel(_cfg(), FakeSender(), update_fetcher=Fetcher(),
                         lease_checker=lambda: False)
    asyncio.run(ch.connect(CancelContext()))
    assert ch.connected is False


def test_media_too_large_sends_receipt():
    sender = FakeSender()

    async def handler(ctx, msg):
        raise AssertionError("over-limit media should not reach handler")

    ch = TelegramChannel(_cfg(handler), sender, bot_username="grape_bot")
    update = {"update_id": 400, "message": {
        "message_id": 400, "chat": {"id": 123, "type": "private"},
        "from": {"id": 42}, "document": {
            "file_id": "huge", "file_name": "big.zip",
            "file_size": 21 * 1024 * 1024}}}

    asyncio.run(ch._on_update(CancelContext(), update))
    assert sender.sent and "20MB" in sender.sent[0].text