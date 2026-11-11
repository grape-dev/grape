"""Telegram 出站测试（Issue #1613 批 1/2/4）——HTTP API / 分块 / HTML 回退 / 媒体。

覆盖：
  - 429 `retry_after` 退避（不洪泛）；401 抛带 code 的 TelegramApiError；
  - 文本 3900 分块、HTML parse_mode、解析失败回退纯文本并计 card_text_fallback；
  - 卡片 `{text, reply_markup}` 直发；附件 sendPhoto/sendDocument；
  - AdapterApi 原地更新 / 表情回执走路由台账；
  - 能力位与空内容护栏。
"""
import asyncio

import pytest

from src.im.adapters.telegram.markdown import (
    TELEGRAM_TEXT_LIMIT,
    format_html,
    split_text,
)
from src.im.adapters.telegram.outbound import (
    TELEGRAM_CAPABILITIES,
    TelegramAdapterApi,
    TelegramApiError,
    TelegramClient,
    TelegramSender,
)
from src.im.adapters.telegram.resolvers import TelegramRouteRegistry
from src.im.channel.capability import Capability
from src.im.channel.message import OutboundMessage


class StubClient:
    """记录调用；可配置 method → (异常/结果) 队列。"""

    def __init__(self):
        self.calls = []
        self.get_file_calls = []
        self.download_calls = []
        self.media_calls = []
        self._responses = {}
        self._file = {"file_path": "photos/a.jpg", "file_size": 3}

    def queue(self, method, *responses):
        self._responses[method] = list(responses)

    async def call(self, method, payload=None, **_kw):
        self.calls.append({"method": method, "payload": payload or {}})
        queue = self._responses.get(method)
        if queue:
            value = queue.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return {"message_id": len(self.calls)}

    async def get_file(self, file_id):
        self.get_file_calls.append(file_id)
        return dict(self._file)

    async def get_updates(self, offset, **_kw):
        return []

    async def download_file(self, file_path):
        self.download_calls.append(file_path)
        return b"abc"

    async def send_media(self, method, chat_id, **kw):
        self.media_calls.append({"method": method, "chat_id": chat_id, **kw})
        return {"message_id": 99}


def test_format_html_converts_common_markdown():
    html = format_html("**粗** _斜_ [x](https://a/b) `code` <tag> &")
    assert "<b>粗</b>" in html
    assert "<i>斜</i>" in html
    assert '<a href="https://a/b">x</a>' in html
    assert "<code>code</code>" in html
    assert "&lt;tag&gt;" in html and "&amp;" in html


def test_split_text_under_limit():
    text = "长" * (TELEGRAM_TEXT_LIMIT * 2 + 30)
    chunks = split_text(text)
    assert len(chunks) >= 3
    assert all(len(chunk) <= TELEGRAM_TEXT_LIMIT for chunk in chunks)
    assert "".join(chunks) == text


def test_client_429_retries_with_retry_after():
    responses = [
        (429, {}, {"ok": False, "error_code": 429,
                   "parameters": {"retry_after": 2}}),
        (200, {}, {"ok": True, "result": {"message_id": 1}}),
    ]
    sleeps = []

    async def http_post(url, payload, timeout):
        return responses.pop(0)

    async def sleep(value):
        sleeps.append(value)

    client = TelegramClient("123:abc", http_post=http_post, sleep=sleep)
    result = asyncio.run(client.call("sendMessage", {"chat_id": 1, "text": "x"}))
    assert result == {"message_id": 1}
    assert sleeps == [2]
    assert client.rate_limited == 1


def test_client_401_raises_with_code():
    async def http_post(url, payload, timeout):
        return (401, {}, {"ok": False, "error_code": 401,
                          "description": "Unauthorized"})

    client = TelegramClient("123:abc", http_post=http_post)
    with pytest.raises(TelegramApiError) as err:
        asyncio.run(client.call("getMe"))
    assert err.value.code == 401


def test_send_text_uses_html_and_fallback_on_parse_error():
    client = StubClient()
    client.queue("sendMessage", TelegramApiError(
        "sendMessage failed: can't parse entities", code=400,
        description="Bad Request: can't parse entities"))
    sender = TelegramSender(client, min_interval=0)

    async def run():
        return await sender.send(OutboundMessage(chat_id="1", text="**x**"))

    result = asyncio.run(run())
    assert result.ok is True
    assert sender.text_fallback == 1
    assert client.calls[0]["payload"]["parse_mode"] == "HTML"
    assert "parse_mode" not in client.calls[1]["payload"]


def test_send_card_passes_reply_markup():
    client = StubClient()
    sender = TelegramSender(client, min_interval=0)
    out = OutboundMessage(chat_id="1", text="", metadata={"raw_card": {
        "text": "<b>x</b>",
        "reply_markup": {"inline_keyboard": [[{"text": "a", "callback_data": "h"}]]},
        "parse_mode": "HTML",
    }})
    result = asyncio.run(sender.send(out))
    assert result.ok is True
    payload = client.calls[0]["payload"]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "h"


def test_send_photo_attachment_uses_send_photo():
    client = StubClient()
    sender = TelegramSender(client, min_interval=0)
    out = OutboundMessage(chat_id="1", text="图",
                          metadata={"attachments": [
                              {"data": b"img", "filename": "a.png"}]})
    result = asyncio.run(sender.send(out))
    assert result.ok is True
    assert client.media_calls[0]["method"] == "sendPhoto"


def test_send_media_url_uses_photo_document_key():
    captured = []

    async def http_post(url, payload, timeout):
        captured.append({"url": url, "payload": payload})
        return 200, {}, {"ok": True, "result": {"message_id": 5}}

    client = TelegramClient("123:abc", http_post=http_post)
    asyncio.run(client.send_media(
        "sendPhoto", "1", url="https://example.com/a.jpg", caption="图"))
    assert captured[0]["payload"]["photo"] == "https://example.com/a.jpg"
    assert captured[0]["payload"]["caption"] == "图"
    assert "file_id" not in captured[0]["payload"]

    asyncio.run(client.send_media(
        "sendDocument", "1", url="https://example.com/a.pdf"))
    assert captured[1]["payload"]["document"] == "https://example.com/a.pdf"
    assert "file_id" not in captured[1]["payload"]


def test_multipart_upload_retries_same_file_after_429():
    responses = [
        (429, {}, {"ok": False, "error_code": 429,
                   "parameters": {"retry_after": 3}}),
        (200, {}, {"ok": True, "result": {"message_id": 7}}),
    ]
    multipart_calls = []
    sleeps = []

    async def http_multipart(url, fields, file_field, filename, data, timeout):
        multipart_calls.append({"file_field": file_field, "filename": filename,
                                "data": data, "fields": dict(fields)})
        return responses.pop(0)

    async def sleep(value):
        sleeps.append(value)

    client = TelegramClient("123:abc", http_multipart=http_multipart, sleep=sleep)
    result = asyncio.run(client.send_media(
        "sendPhoto", "1", data=b"img", filename="a.png"))

    assert result == {"message_id": 7}
    assert sleeps == [3]
    assert client.rate_limited == 1
    # 两次都必须是带文件的同一份 multipart，不能退化为 JSON
    assert len(multipart_calls) == 2
    assert all(c["data"] == b"img" and c["file_field"] == "photo"
               for c in multipart_calls)


def test_empty_content_guard():
    sender = TelegramSender(StubClient(), min_interval=0)
    result = asyncio.run(sender.send(OutboundMessage(chat_id="1")))
    assert result.ok is False and "empty" in result.error


def test_adapter_api_edit_and_reaction_use_route_registry():
    client = StubClient()
    sender = TelegramSender(client, min_interval=0)
    routes = TelegramRouteRegistry()
    routes.remember("7", "123")
    api = TelegramAdapterApi("123:abc", client=client, sender=sender,
                             route_registry=routes)

    async def run():
        ok = await api.update_card_by_token(
            None, "123|7", {"text": "已绑定", "reply_markup": {}})
        reaction = await api.add_reaction(None, "7", "OK")
        return ok, reaction

    ok, reaction = asyncio.run(run())
    assert ok is True and reaction is True
    methods = [call["method"] for call in client.calls]
    assert "editMessageText" in methods and "setMessageReaction" in methods


def test_adapter_api_downloads_media_via_get_file():
    client = StubClient()
    api = TelegramAdapterApi("123:abc", client=client)

    data = asyncio.run(api.download_image(None, "file-id"))
    assert data == b"abc"
    assert client.get_file_calls == ["file-id"]
    assert client.download_calls == ["photos/a.jpg"]
    # 二次读取命中缓存，不再打网络
    asyncio.run(api.download_image(None, "file-id"))
    assert client.get_file_calls == ["file-id"]


def test_capabilities_full_contract():
    assert TELEGRAM_CAPABILITIES & Capability.TEXT
    assert TELEGRAM_CAPABILITIES & Capability.RICH_CARD
    assert TELEGRAM_CAPABILITIES & Capability.CARD_ACTION
    assert TELEGRAM_CAPABILITIES & Capability.MESSAGE_EDIT
    assert TELEGRAM_CAPABILITIES & Capability.REACTION
