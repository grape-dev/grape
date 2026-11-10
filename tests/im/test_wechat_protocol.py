"""微信 ilink 协议层测试（Issue #1728 M1）。

覆盖：请求头/鉴权、base_info、版本编码、容错访问器、媒体描述解析、
`ret` 语义与 HTTP 客户端重试/`ret=-14` 暂停标记、4000 分块边界。
"""
import asyncio
import json

import pytest

from src.im.adapters.wechat.outbound import split_text
from src.im.adapters.wechat.protocol import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_TEXT,
    ITEM_VOICE,
    RET_SESSION_PAUSED,
    TEXT_LIMIT,
    WechatApiError,
    WechatClient,
    as_dict,
    as_int,
    base_info,
    build_headers,
    context_token_of,
    extract_text,
    from_user_of,
    item_text,
    message_id_of,
    message_type_of,
    parse_media,
    ret_of,
    version_code,
)


def test_headers_carry_bearer_and_protocol_fields():
    headers = build_headers("tok-1", uin="dWlu")
    assert headers["Authorization"] == "Bearer tok-1"
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert headers["iLink-App-Id"] == "bot"
    assert headers["X-WECHAT-UIN"] == "dWlu"
    assert headers["Content-Type"] == "application/json"


def test_headers_without_token_have_no_authorization():
    headers = build_headers("")
    assert "Authorization" not in headers
    assert "AuthorizationType" not in headers


def test_base_info_identifies_grape_agent():
    info = base_info()
    assert info["bot_agent"].startswith("grape")
    assert "channel_version" in info


def test_version_code_monotonic():
    assert version_code("1.0.0") < version_code("1.1.0") < version_code("2.0.0")


def test_tolerant_accessors_never_raise():
    assert as_dict(None) == {}
    assert as_int("x", 7) == 7
    assert ret_of({"errcode": -14}) == RET_SESSION_PAUSED
    assert ret_of({}) == 0
    assert message_id_of({"message_id": 1}) == "1"
    assert from_user_of({}) == ""
    assert message_type_of({"message_type": 2}) == 2
    assert context_token_of({"context_token": "ctx"}) == "ctx"


def test_item_text_and_extract_text():
    items = [
        {"type": ITEM_TEXT, "text_item": {"text": "hello"}},
        {"type": ITEM_TEXT, "text_item": {"text": "world"}},
    ]
    assert extract_text(items) == "hello\nworld"
    assert item_text({"type": ITEM_VOICE, "voice_item": {"text": "语音转写"}}) == "语音转写"
    assert item_text({"type": ITEM_IMAGE}) == ""


def test_parse_media_image_hex_and_file():
    image = parse_media({"type": ITEM_IMAGE, "image_item": {
        "aeskey": "a" * 32,
        "media": {"full_url": "https://cdn/x", "encrypt_type": 1}}})
    assert image["url"] == "https://cdn/x"
    assert image["aes_key"] == "a" * 32
    assert image["type"] == ITEM_IMAGE

    file_item = parse_media({"type": ITEM_FILE, "file_item": {
        "file_name": "a.pdf",
        "media": {"encrypt_query_param": "qp"}}})
    assert file_item["filename"] == "a.pdf"
    assert "encrypted_query_param=qp" in file_item["url"]


def test_split_text_boundaries():
    assert split_text("x" * (TEXT_LIMIT - 1)) == ["x" * (TEXT_LIMIT - 1)]
    assert split_text("x" * TEXT_LIMIT) == ["x" * TEXT_LIMIT]
    chunks = split_text("x" * (TEXT_LIMIT + 1))
    assert len(chunks) == 2
    assert all(len(chunk) <= TEXT_LIMIT for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) == TEXT_LIMIT + 1


def test_split_text_prefers_newline():
    text = "a" * 2500 + "\n" + "b" * 2500
    chunks = split_text(text)
    assert chunks[0] == "a" * 2500
    assert chunks[1] == "b" * 2500


def _client_with(handler, **kwargs):
    calls = []

    async def http_post(url, payload, headers, timeout):
        calls.append((url, payload, headers))
        return handler(url, payload)

    client = WechatClient("tok", http_post=http_post,
                          sleep=_no_sleep, **kwargs)
    return client, calls


async def _no_sleep(_delay):
    return None


def test_client_ret_nonzero_raises():
    client, _ = _client_with(lambda url, payload: (200, {}, json.dumps({"ret": 5}).encode()))
    with pytest.raises(WechatApiError) as exc:
        asyncio.run(client.send_message(to_user_id="u", text="hi"))
    assert exc.value.ret == 5


def test_client_ret_minus_14_marks_session_paused():
    client, _ = _client_with(lambda url, payload: (200, {}, json.dumps({"ret": -14}).encode()))
    with pytest.raises(WechatApiError) as exc:
        asyncio.run(client.get_updates(""))
    assert exc.value.session_paused is True


def test_client_retries_5xx_then_succeeds():
    responses = [(500, {}, b""), (502, {}, b""), (200, {}, json.dumps({"ret": 0}).encode())]

    def handler(url, payload):
        return responses.pop(0)

    client, calls = _client_with(handler, max_retries=3)
    asyncio.run(client.notify_start())
    assert len(calls) == 3


def test_client_exhausted_retries_raises():
    client, calls = _client_with(lambda url, payload: (500, {}, b""), max_retries=1)
    with pytest.raises(WechatApiError):
        asyncio.run(client.notify_start())
    assert len(calls) == 2


def test_send_message_payload_shape():
    client, calls = _client_with(
        lambda url, payload: (200, {}, json.dumps({"ret": 0, "msg": {"message_id": 9}}).encode()))
    result = asyncio.run(client.send_message(
        to_user_id="u@im.wechat", text="hello", client_id="cid-1", context_token="ctx-1"))
    msg = calls[0][1]["msg"]
    assert msg["to_user_id"] == "u@im.wechat"
    assert msg["client_id"] == "cid-1"
    assert msg["context_token"] == "ctx-1"
    assert msg["message_type"] == 2 and msg["message_state"] == 2
    assert msg["item_list"][0]["text_item"]["text"] == "hello"
    assert calls[0][1]["base_info"]["bot_agent"].startswith("grape")
    assert result["msg"]["message_id"] == 9