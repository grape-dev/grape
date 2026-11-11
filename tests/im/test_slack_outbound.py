"""Slack 出站测试（Issue #1309 §8.3 / §8.5 / §8.6）。

覆盖：
  - 标准 Markdown → mrkdwn（粗体/链接/删除线/标题；代码块原样）；
  - 38000 字符分块：中文不切在多字节中间、边界安全、拼接可还原；
  - 线程：`thread_root` / `reply_to` → `thread_ts`；
  - blocks 卡片载荷 + `chat.update` 原地更新 + `reactions.add` 名称映射；
  - 429 + `Retry-After` 退避重试（≤3 次），连续 429 计 failed；
  - 文件三步上传（`getUploadURLExternal` → PUT → `completeUploadExternal`，禁 `files.upload`）；
  - 空内容硬护栏、lark 卡片透传护栏；
  - SlackAdapterApi：媒体带 token 下载 + 缓存、ts→channel 反查更新。
"""
import asyncio

import pytest

from src.im.adapters.slack.markdown import (
    SLACK_TEXT_LIMIT,
    format_mrkdwn,
    split_text,
)
from src.im.adapters.slack.outbound import (
    SlackAdapterApi,
    SlackApiError,
    SlackClient,
    SlackSender,
)
from src.im.adapters.slack.resolvers import SlackMediaCache, SlackMediaFetcher, SlackRouteRegistry
from src.im.channel.message import OutboundMessage, ReplyCtx

BOT = "xoxb-test"


class FakeHttp:
    """按序返回 (status, headers, body)，并记录每次请求。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    async def __call__(self, url, payload, headers, timeout):
        self.calls.append({"url": url, "payload": payload, "headers": headers})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return item


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _client(responses, *, max_retries=3, sleeps=None):
    http = FakeHttp(responses)
    clock = FakeClock()
    sleeps = sleeps if sleeps is not None else []

    async def _sleep(delay):
        sleeps.append(delay)
        clock.now += delay

    client = SlackClient(BOT, http_post=http, max_retries=max_retries,
                         sleep=_sleep, clock=clock)
    return client, http, sleeps, clock


# ── mrkdwn ─────────────────────────────────────────────────

def test_mrkdwn_bold_italic_link_strike():
    out = format_mrkdwn("**粗体** 和 _斜_ 与 ~~删~~ 与 [文字](https://example.com/a)")
    assert out == "*粗体* 和 _斜_ 与 ~删~ 与 <https://example.com/a|文字>"


def test_mrkdwn_heading_becomes_bold_line():
    assert format_mrkdwn("## 标题行") == "*标题行*"


def test_mrkdwn_code_block_preserved():
    src = "说明\n```\n**not bold** [x](y)\n```\n结束"
    out = format_mrkdwn(src)
    assert "**not bold** [x](y)" in out


def test_mrkdwn_escapes_angle_and_ampersand():
    out = format_mrkdwn("a < b && c > d")
    assert out == "a &lt; b &amp;&amp; c &gt; d"


def test_mrkdwn_single_asterisk_is_bold_not_list_bullet():
    """单星号在 mrkdwn 中即粗体：保留原样（避免把已转换粗体二次变斜体）。"""
    assert format_mrkdwn("*粗体*") == "*粗体*"
    assert format_mrkdwn("* 列表项") == "* 列表项"


def test_mrkdwn_is_idempotent():
    """重复调用不叠加改写：粗体/链接/转义二次运行保持稳定。"""
    source = "**粗体** 与 _斜_ 与 ~~删~~ 与 [文字](https://example.com/a) 与 a < b && c > d"
    once = format_mrkdwn(source)
    assert once == ("*粗体* 与 _斜_ 与 ~删~ 与 <https://example.com/a|文字> "
                    "与 a &lt; b &amp;&amp; c &gt; d")
    assert format_mrkdwn(once) == once


# ── 38000 分块 ─────────────────────────────────────────────

def test_split_text_chinese_boundary_safe():
    text = ("中文长文本测试，" * 6000)          # 54 万字符级别
    chunks = split_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= SLACK_TEXT_LIMIT for c in chunks)
    assert "".join(chunks) == text              # 可还原（不切多字节字符）
    assert all("\ufffd" not in c for c in chunks)


def test_split_text_short_text_single_chunk():
    assert split_text("hello") == ["hello"]
    assert split_text("") == []


def test_split_text_closes_code_fence_across_chunks():
    code = "```\n" + ("print(1)\n" * 9000) + "```"
    chunks = split_text(code, limit=4000)
    assert len(chunks) > 1
    for index, chunk in enumerate(chunks):
        if index == 0:
            # 首块围栏未闭合 → 必须补闭合
            assert chunk.count("```") % 2 == 0
        assert len(chunk) <= 4000


# ── 发送 ───────────────────────────────────────────────────

async def test_sender_posts_mrkdwn_text():
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True, "ts": "1.1"})])
    sender = SlackSender(client, min_interval=0.0)
    res = await sender.send(OutboundMessage(chat_id="C1", text="**粗**"))
    assert res.ok and res.message_id == "1.1"
    payload = http.calls[0]["payload"]
    assert payload["channel"] == "C1" and payload["text"] == "*粗*"


async def test_sender_chunks_long_text_and_keeps_thread():
    text = "长" * (SLACK_TEXT_LIMIT * 2 + 100)
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True, "ts": "9.9"})])
    sender = SlackSender(client, min_interval=0.0)
    res = await sender.send(OutboundMessage(
        chat_id="C1", text=text, reply_to=ReplyCtx(message_id="1.5")))
    assert res.ok and res.message_id == "9.9"
    posts = [c["payload"] for c in http.calls]
    assert len(posts) == 3
    assert all(len(p["text"]) <= SLACK_TEXT_LIMIT for p in posts)
    assert all(p["thread_ts"] == "1.5" for p in posts)
    assert "".join(p["text"] for p in posts) == text


async def test_sender_thread_root_wins():
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True, "ts": "2.2"})])
    sender = SlackSender(client, min_interval=0.0)
    await sender.send(OutboundMessage(chat_id="C1", text="hi", thread_root="1.0",
                                      reply_to=ReplyCtx(message_id="1.5")))
    assert http.calls[0]["payload"]["thread_ts"] == "1.0"


async def test_sender_blocks_payload():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*卡*"}}]
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True, "ts": "3.3"})])
    sender = SlackSender(client, min_interval=0.0)
    res = await sender.send(OutboundMessage(
        chat_id="C1", text="回退文本", metadata={"raw_card": {"blocks": blocks}}))
    assert res.ok
    payload = http.calls[0]["payload"]
    assert payload["blocks"] == blocks
    assert payload["text"] == "回退文本"


async def test_sender_empty_content_guard():
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True})])
    sender = SlackSender(client, min_interval=0.0)
    res = await sender.send(OutboundMessage(chat_id="C1", text="   "))
    assert not res.ok and res.error == "empty content guard"
    assert http.calls == []


async def test_sender_refuses_lark_raw_card():
    client, _http, _sleeps, _clock = _client([(200, {}, {"ok": True})])
    sender = SlackSender(client, min_interval=0.0)
    res = await sender.send(OutboundMessage(
        chat_id="C1", text="x", metadata={"raw_card": {"header": {"title": "x"}}}))
    assert not res.ok and "lark raw_card refused" in res.error


async def test_sender_kind_card_builds_header_and_section():
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True, "ts": "4.4"})])
    sender = SlackSender(client, min_interval=0.0)
    res = await sender.send(OutboundMessage(
        chat_id="C1", text="内容", metadata={"kind": "card", "title": "标题"}))
    assert res.ok
    blocks = http.calls[0]["payload"]["blocks"]
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "标题"
    assert blocks[1]["type"] == "section"


# ── 429 退避 ───────────────────────────────────────────────

async def test_429_retry_after_then_success():
    client, http, sleeps, _clock = _client([
        (429, {"Retry-After": "2"}, {"ok": False, "error": "ratelimited"}),
        (200, {}, {"ok": True, "ts": "5.5"}),
    ])
    body = await client.call("chat.postMessage", {"channel": "C1", "text": "hi"})
    assert body["ok"] and body["ts"] == "5.5"
    assert sleeps == [2.0]
    assert client.rate_limited == 1
    assert len(http.calls) == 2


async def test_429_exhausted_raises_and_sender_marks_failed():
    client, http, sleeps, _clock = _client(
        [(429, {"Retry-After": "1"}, {"ok": False, "error": "ratelimited"})], max_retries=2)
    sender = SlackSender(client, min_interval=0.0)
    res = await sender.send(OutboundMessage(chat_id="C1", text="hi"))
    assert not res.ok and "rate limited" in res.error
    assert len(http.calls) == 3          # 1 次 + 2 次重试
    assert sleeps == [1.0, 1.0]


async def test_ok_false_raises_slack_api_error():
    client, _http, _sleeps, _clock = _client([(200, {}, {"ok": False, "error": "invalid_auth"})])
    with pytest.raises(SlackApiError) as exc:
        await client.call("chat.postMessage", {"channel": "C1", "text": "hi"})
    assert "invalid_auth" in str(exc.value)


# ── 更新 / 表情 / 上传 ─────────────────────────────────────

async def test_chat_update_payload():
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True, "ts": "6.6"})])
    sender = SlackSender(client, min_interval=0.0)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "✅ 已绑定"}}]
    await sender.update("C1", "6.6", blocks=blocks)
    call = http.calls[0]
    assert call["url"].endswith("/chat.update")
    assert call["payload"]["channel"] == "C1" and call["payload"]["ts"] == "6.6"
    assert call["payload"]["blocks"] == blocks


async def test_reaction_name_mapping():
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True})])
    sender = SlackSender(client, min_interval=0.0)
    assert await sender.add_reaction("C1", "1.1", "OK") is True
    assert http.calls[0]["payload"]["name"] == "white_check_mark"


async def test_file_upload_three_steps_no_files_upload():
    uploads: list = []

    async def _upload(url, data, filename, timeout):
        uploads.append((url, data, filename))

    http = FakeHttp([
        (200, {}, {"ok": True, "upload_url": "https://files.slack.com/upload/v1/abc",
                   "file_id": "F123"}),
        (200, {}, {"ok": True, "files": [{"id": "F123"}]}),
    ])
    client = SlackClient(BOT, http_post=http, http_upload=_upload)
    client.rate_limited = 0
    file_id = await client.upload_file(b"PNGDATA", "shot.png",
                                       channel="C1", thread_ts="1.1", title="截图")
    assert file_id == "F123"
    assert uploads and uploads[0][1] == b"PNGDATA"
    methods = [c["payload"].get("channel_id") is not None for c in http.calls]
    assert methods == [False, True]
    complete = http.calls[1]["payload"]
    assert complete["files"][0]["id"] == "F123"
    assert complete["channel_id"] == "C1" and complete["thread_ts"] == "1.1"
    assert not any("files.upload" in call["url"] for call in http.calls)


async def test_sender_uploads_attachment():
    uploads: list = []

    async def _upload(url, data, filename, timeout):
        uploads.append(filename)

    http = FakeHttp([
        (200, {}, {"ok": True, "ts": "8.8"}),          # 正文消息
        (200, {}, {"ok": True, "upload_url": "https://files.slack.com/upload/v1/abc",
                   "file_id": "F9"}),
        (200, {}, {"ok": True}),
    ])
    client = SlackClient(BOT, http_post=http, http_upload=_upload)
    sender = SlackSender(client, min_interval=0.0)
    res = await sender.send(OutboundMessage(
        chat_id="C1", text="产物", metadata={"attachments": [
            {"data": b"BIN", "filename": "out.log"}]}))
    assert res.ok and uploads == ["out.log"]


# ── 节流 ───────────────────────────────────────────────────

async def test_sender_throttles_per_channel():
    client, http, sleeps, clock = _client([(200, {}, {"ok": True, "ts": "1"})])

    async def _sleep(delay):
        sleeps.append(delay)
        clock.now += delay

    sender = SlackSender(client, min_interval=1.0, clock=clock, sleep=_sleep)
    first = OutboundMessage(chat_id="C1", text="a")
    second = OutboundMessage(chat_id="C1", text="b")
    await sender.send(first)
    await sender.send(second)
    assert sleeps == [1.0]
    assert len(http.calls) == 2


# ── AdapterApi ─────────────────────────────────────────────

async def test_adapter_media_download_uses_token_and_cache():
    fetched: list = []

    async def _get(url, token):
        fetched.append((url, token))
        return b"IMG"

    client, _http, _sleeps, _clock = _client([(200, {}, {"ok": True})])
    media = SlackMediaFetcher(BOT, http_get=_get)
    api = SlackAdapterApi(BOT, client=client, media=media)
    assert await api.download_image(None, "https://files.slack.com/x.png") == b"IMG"
    assert await api.download_image(None, "https://files.slack.com/x.png") == b"IMG"
    assert len(fetched) == 1                 # 二次命中缓存
    assert fetched[0][1] == BOT              # 带 xoxb


async def test_adapter_update_message_uses_route_registry():
    routes = SlackRouteRegistry()
    routes.remember("1.1", "C1")
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True, "ts": "1.1"})])
    sender = SlackSender(client, min_interval=0.0)
    api = SlackAdapterApi(BOT, client=client, sender=sender, route_registry=routes)
    assert await api.update_message(None, "1.1", "**新**内容") is True
    assert http.calls[0]["payload"]["text"] == "*新*内容"


async def test_adapter_update_card_by_token():
    client, http, _sleeps, _clock = _client([(200, {}, {"ok": True, "ts": "1.1"})])
    sender = SlackSender(client, min_interval=0.0)
    api = SlackAdapterApi(BOT, client=client, sender=sender)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "✅"}}]
    assert await api.update_card_by_token(None, "C1|1.1", {"blocks": blocks}) is True
    payload = http.calls[0]["payload"]
    assert payload["channel"] == "C1" and payload["ts"] == "1.1"
    assert payload["blocks"] == blocks


async def test_adapter_send_text_to_user_opens_dm():
    client, http, _sleeps, _clock = _client([
        (200, {}, {"ok": True, "channel": {"id": "D999"}}),
        (200, {}, {"ok": True, "ts": "7.7"}),
    ])
    sender = SlackSender(client, min_interval=0.0)
    api = SlackAdapterApi(BOT, client=client, sender=sender)
    msg_id = await api.send_text_to_user(None, "U123", "hello")
    assert msg_id == "7.7"
    assert http.calls[0]["payload"] == {"users": "U123"}
    assert http.calls[1]["payload"]["channel"] == "D999"


def test_media_cache_bounded_and_expiring():
    clock = FakeClock()
    cache = SlackMediaCache(ttl=10.0, max_entries=2, clock=clock)
    cache.put("a", b"1")
    cache.put("b", b"2")
    cache.put("c", b"3")
    assert len(cache) == 2                    # 有界
    clock.now = 100.0
    assert cache.get("c") == b""              # 过期
