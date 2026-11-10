"""微信出站测试（Issue #1728 M1/M2）——4000 分块 + client_id 幂等 + context_token。"""
import json

import pytest

from src.im.adapters.wechat.outbound import (
    WECHAT_CAPABILITIES,
    WechatSender,
    split_text,
)
from src.im.adapters.wechat.protocol import TEXT_LIMIT, WechatApiError, WechatClient
from src.im.adapters.wechat.resolvers import WechatRouteRegistry
from src.im.channel.capability import Capability
from src.im.channel.message import OutboundMessage


def _client(responses=None, calls=None):
    responses = list(responses or [])

    async def http_post(url, payload, headers, timeout):
        if calls is not None:
            calls.append((url, payload))
        body = responses.pop(0) if responses else {"ret": 0, "msg": {"message_id": 1}}
        return 200, {}, json.dumps(body).encode()

    return WechatClient("tok", http_post=http_post, sleep=_no_sleep)


async def _no_sleep(_delay):
    return None


def test_capabilities_are_text_rich_card_attachment_only():
    assert WECHAT_CAPABILITIES & Capability.TEXT
    assert WECHAT_CAPABILITIES & Capability.RICH_CARD
    assert WECHAT_CAPABILITIES & Capability.ATTACHMENT
    assert not (WECHAT_CAPABILITIES & Capability.CARD_ACTION)
    assert not (WECHAT_CAPABILITIES & Capability.MESSAGE_EDIT)
    assert not (WECHAT_CAPABILITIES & Capability.REACTION)


def test_split_boundary_lengths():
    assert split_text("a" * (TEXT_LIMIT - 1)) == ["a" * (TEXT_LIMIT - 1)]
    assert split_text("a" * TEXT_LIMIT) == ["a" * TEXT_LIMIT]
    chunks = split_text("a" * (TEXT_LIMIT + 1))
    assert len(chunks) == 2 and all(len(c) <= TEXT_LIMIT for c in chunks)


def test_send_chunks_long_text_with_unique_client_ids():
    calls = []
    client = _client(calls=calls)
    sender = WechatSender(client)
    out = OutboundMessage(chat_id="u@im.wechat", text="a" * (TEXT_LIMIT * 2 + 10))
    result = __import__("asyncio").run(sender.send(out))
    assert result.ok
    send_calls = [p for u, p in calls if u.endswith("/sendmessage")]
    assert len(send_calls) == 3
    client_ids = [p["msg"]["client_id"] for p in send_calls]
    assert len(set(client_ids)) == 3
    assert all(p["msg"]["to_user_id"] == "u@im.wechat" for p in send_calls)


def test_send_includes_context_token_from_inbound_route():
    calls = []
    client = _client(calls=calls)
    routes = WechatRouteRegistry()
    routes.remember("u@im.wechat", context_token="ctx-9", message_id="m1")
    sender = WechatSender(client, routes=routes)
    __import__("asyncio").run(sender.send(OutboundMessage(chat_id="u@im.wechat", text="hi")))
    send_calls = [p for u, p in calls if u.endswith("/sendmessage")]
    assert send_calls[0]["msg"]["context_token"] == "ctx-9"


def test_send_raw_card_text_payload():
    calls = []
    client = _client(calls=calls)
    sender = WechatSender(client)
    out = OutboundMessage(chat_id="u", text="", metadata={
        "raw_card": {"text": "【标题】\n1. 选项"}})
    result = __import__("asyncio").run(sender.send(out))
    assert result.ok
    send_calls = [p for u, p in calls if u.endswith("/sendmessage")]
    assert send_calls[0]["msg"]["item_list"][0]["text_item"]["text"].startswith("【标题】")


def test_empty_guard_returns_failed():
    sender = WechatSender(_client())
    result = __import__("asyncio").run(sender.send(OutboundMessage(chat_id="u", text="  ")))
    assert result.ok is False
    assert "empty" in result.error


def test_empty_route_returns_failed():
    sender = WechatSender(_client())
    result = __import__("asyncio").run(sender.send(OutboundMessage(chat_id="", text="hi")))
    assert result.ok is False


def test_send_result_reports_api_error():
    async def http_post(url, payload, headers, timeout):
        return 200, {}, json.dumps({"ret": 3, "errmsg": "bad"}).encode()

    sender = WechatSender(WechatClient("tok", http_post=http_post, sleep=_no_sleep))
    result = __import__("asyncio").run(sender.send(OutboundMessage(chat_id="u", text="hi")))
    assert result.ok is False
    assert "ret=3" in result.error


class FakeMetrics:
    def __init__(self):
        self.reasons = []

    def record_degraded(self, reason=""):
        self.reasons.append(reason)


class FailingUploader:
    async def upload(self, *args, **kwargs):
        raise WechatApiError("upload boom")


def test_media_upload_failure_falls_back_to_text_and_counts():
    calls = []
    metrics = FakeMetrics()
    client = _client(calls=calls)
    sender = WechatSender(client, uploader=FailingUploader(), metrics=metrics)
    out = OutboundMessage(chat_id="u", text="", metadata={
        "attachments": [{"filename": "a.pdf", "data": b"123"}]})
    result = __import__("asyncio").run(sender.send(out))
    assert result.ok
    assert "media_upload_failed" in metrics.reasons
    send_calls = [p for u, p in calls if u.endswith("/sendmessage")]
    assert "附件发送失败" in send_calls[-1]["msg"]["item_list"][0]["text_item"]["text"]


class FakeUploader:
    def __init__(self):
        self.calls = []

    async def upload(self, data, filename="file.bin", media_type="file"):
        self.calls.append((data, filename, media_type))
        return {"encrypt_query_param": "qp", "aes_key": "a2V5", "encrypt_type": 1}


def test_media_success_sends_media_item():
    calls = []
    client = _client(calls=calls)
    uploader = FakeUploader()
    sender = WechatSender(client, uploader=uploader)
    out = OutboundMessage(chat_id="u", text="", metadata={
        "attachments": [{"filename": "a.png", "mime_type": "image/png", "data": b"img"}]})
    result = __import__("asyncio").run(sender.send(out))
    assert result.ok
    assert uploader.calls[0][2] == "image"
    send_calls = [p for u, p in calls if u.endswith("/sendmessage")]
    item = send_calls[-1]["msg"]["item_list"][0]
    assert item["type"] == 2
    assert item["image_item"]["media"]["encrypt_query_param"] == "qp"