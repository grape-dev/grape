"""Slack 出站 —— OutboundMessage → Slack Web API（Issue #1309 §3.3 / §4.3）。

三条硬约束（官方文档 + 设计文档 §3.3）：

1. **发送走 Web API `chat.postMessage`**（Bot Token `xoxb-`），单条 text 硬上限约 40k
   字符 → 沿用上游口径按 **38000 字符** 分块（markdown.py），中文不切多字节。
2. **Tier 3 限流**（约 1 msg/s/通道，短时可突发）：发送前节流 + 429 `Retry-After`
   退避重试（≤3 次），仍失败 → SendResult(ok=False) 由 CountingSender 计 failed。
3. **文件上传只走三步新流程**：`files.getUploadURLExternal` → PUT → `files.completeUploadExternal`
   （旧 `files.upload` 已弃用，直接 410/拒绝）。

线程策略：`thread_root`（Slack `thread_ts`）存在 → 落线程；否则顶层。
卡片：`metadata.raw_card = {"blocks": [...]}` 直接发 Block Kit；`chat.update` 原地更新。

失败语义（继承 #1008）：任何传输/`ok=false` 失败都返回 SendResult(ok=False, error=...)，
不静默计成功；空内容硬护栏与 `card_empty_guard` 同口径。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ...channel.capability import Capability
from ...channel.message import OutboundMessage, SendResult
from .cards import MAX_ACTION_ELEMENTS, MAX_BLOCKS, card_token, parse_card_token
from .markdown import SLACK_TEXT_LIMIT, format_mrkdwn, split_text
from .resolvers import SlackMediaFetcher, SlackRouteRegistry, SlackUserResolver

log = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"
DEFAULT_TIMEOUT = 12.0
MAX_RATE_LIMIT_RETRIES = 3
DEFAULT_MIN_INTERVAL = 1.0        # Tier 3：每通道 ~1 msg/s
MAX_FALLBACK_TEXT = 3000          # blocks 消息的通知回退文本

# 能力位申报（设计 §4.1，全渠道最全的一组）
# 不申报：QUOTE_REPLY（Slack 用线程表达"针对某条"，不是引用块）、TYPING_INDICATOR、
#        VOICE（v1 不做语音转写，收到语音按文件处理）
SLACK_CAPABILITIES = (Capability.TEXT | Capability.RICH_CARD | Capability.CARD_ACTION
                      | Capability.THREAD_REPLY | Capability.MESSAGE_EDIT
                      | Capability.ATTACHMENT | Capability.REACTION)

# 引擎口径 emoji → Slack reactions.add 的 name（Slack 用小写短名，不带冒号）
_REACTION_NAMES = {
    "OK": "white_check_mark",
    "ok": "white_check_mark",
    "OK_HAND": "ok_hand",
    "THUMBSUP": "+1",
    "THUMBS_UP": "+1",
    "EYES": "eyes",
}


class SlackApiError(RuntimeError):
    """Slack Web API 失败（传输 / `ok=false` / 限流重试耗尽）。"""


# ---------------------------------------------------------------- HTTP 传输

async def _default_http_post(url: str, payload: dict, headers: dict,
                             timeout: float) -> tuple[int, dict, dict]:
    """POST JSON：返回 (status, headers, body)。headers/body 均可注入替身。"""
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}
    return resp.status_code, dict(resp.headers), (body if isinstance(body, dict) else {})


async def _default_http_upload(url: str, data: bytes, filename: str,
                               timeout: float) -> None:
    """把文件 PUT/POST 到外链上传地址（官方示例：multipart 字段名 file）。"""
    import httpx
    files = {"file": (filename or "file.bin", data, "application/octet-stream")}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, files=files)
    if resp.status_code not in (200, 201):
        raise SlackApiError(f"file upload HTTP {resp.status_code}: {(resp.text or '')[:120]}")


def _retry_after_seconds(status_headers: dict, body: dict) -> float:
    raw = (status_headers or {}).get("Retry-After") or (status_headers or {}).get("retry-after")
    if raw is None and isinstance(body, dict):
        raw = body.get("retry_after")
    try:
        value = float(raw) if raw is not None else 1.0
    except Exception:
        value = 1.0
    return max(0.0, min(value, 60.0))


class SlackClient:
    """Web API 客户端：Bearer 鉴权 + 429 退避 + 三步上传 + 私有文件下载。

    注入点：http_post / http_upload / http_get / sleep / clock —— 单测全程不触网。
    """

    def __init__(self, token: str, *,
                 http_post: Callable[..., Awaitable[tuple[int, dict, dict]]] | None = None,
                 http_upload: Callable[..., Awaitable[None]] | None = None,
                 http_get: Callable[[str, str], Awaitable[bytes]] | None = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = MAX_RATE_LIMIT_RETRIES,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._token = str(token or "")
        self._http_post = http_post or _default_http_post
        self._http_upload = http_upload or _default_http_upload
        self._http_get = http_get
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._sleep = sleep
        self._clock = clock
        self.calls: list[dict] = []      # 测试观察哨：真实发出的 API 调用
        self.rate_limited = 0

    @property
    def token(self) -> str:
        return self._token

    async def call(self, method: str, payload: dict | None = None) -> dict:
        """POST /api/<method>：`ok=false` 抛 SlackApiError；429 按 Retry-After 退避。"""
        url = f"{SLACK_API_BASE}/{method}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        body_in = payload or {}
        attempt = 0
        while True:
            status, resp_headers, body = await self._http_post(
                url, body_in, headers, self._timeout)
            # 观察哨不落 token：只记 method 与关键路由字段
            self.calls.append({"method": method, "payload": _redacted_payload(body_in),
                               "status": status})
            if status == 429 or (isinstance(body, dict) and body.get("error") == "ratelimited"):
                self.rate_limited += 1
                if attempt >= self._max_retries:
                    raise SlackApiError(
                        f"{method} rate limited after {attempt + 1} attempts")
                delay = _retry_after_seconds(resp_headers, body if isinstance(body, dict) else {})
                log.info("[slack] %s 429, retry in %.1fs (attempt %s)", method, delay, attempt + 1)
                await self._sleep(delay)
                attempt += 1
                continue
            if status >= 400:
                raise SlackApiError(f"{method} HTTP {status}: {str(body)[:200]}")
            if not isinstance(body, dict):
                raise SlackApiError(f"{method} returned non-object body")
            if not body.get("ok"):
                raise SlackApiError(f"{method} failed: {body.get('error') or 'unknown_error'}")
            return body

    async def download(self, url: str) -> bytes:
        """私有文件下载（url_private_download 必须带 xoxb）。"""
        if not url:
            return b""
        if self._http_get is not None:
            return bytes(await self._http_get(url, self._token) or b"")
        import httpx
        headers = {"Authorization": f"Bearer {self._token}"}
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise SlackApiError(f"file download HTTP {resp.status_code}")
        return bytes(resp.content or b"")

    async def upload_file(self, data: bytes, filename: str, *,
                          channel: str = "", thread_ts: str = "",
                          title: str = "", initial_comment: str = "") -> str:
        """三步上传：getUploadURLExternal → PUT → completeUploadExternal（禁 files.upload）。"""
        if not data:
            raise SlackApiError("empty media payload")
        name = (filename or "file.bin")[:255]
        init = await self.call("files.getUploadURLExternal",
                               {"filename": name, "length": len(data)})
        upload_url = str(init.get("upload_url") or "")
        file_id = str(init.get("file_id") or "")
        if not upload_url or not file_id:
            raise SlackApiError("files.getUploadURLExternal returned no upload_url/file_id")
        await self._http_upload(upload_url, data, name, self._timeout)
        payload: dict[str, Any] = {
            "files": [{"id": file_id, "title": (title or name)[:255]}],
        }
        if channel:
            payload["channel_id"] = channel
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if initial_comment:
            payload["initial_comment"] = initial_comment[:SLACK_TEXT_LIMIT]
        await self.call("files.completeUploadExternal", payload)
        return file_id


def _redacted_payload(payload: dict) -> dict:
    """观察哨副本：抹掉 token 类字段（永不落内存日志/断言副本）。"""
    if not isinstance(payload, dict):
        return {}
    redacted = dict(payload)
    for key in ("token", "app_token", "bot_token"):
        if key in redacted:
            redacted[key] = "***"
    return redacted


# ---------------------------------------------------------------- Sender

class SlackSender:
    """出站发送器：文本（38000 分块）+ 卡片（blocks）+ 更新/表情/上传。"""

    def __init__(self, client: SlackClient, *,
                 min_interval: float = DEFAULT_MIN_INTERVAL,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._client = client
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._last_sent: dict[str, float] = {}

    def capabilities(self) -> Capability:
        return SLACK_CAPABILITIES

    # ---------------- 节流 / 路由

    async def _throttle(self, channel: str) -> None:
        """Tier 3 节流：同通道两次发送间至少 min_interval 秒。"""
        if self._min_interval <= 0:
            self._last_sent[channel] = self._clock()
            return
        last = self._last_sent.get(channel)
        now = self._clock()
        if last is not None:
            wait = self._min_interval - (now - last)
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
        self._last_sent[channel] = now

    @staticmethod
    def thread_ts_of(out: OutboundMessage) -> str:
        """出站线程根：显式 thread_root 优先，其次回复目标消息 ts（Slack 回复=同线程）。"""
        root = str(out.thread_root or "").strip()
        if root:
            return root
        reply_id = str(getattr(out.reply_to, "message_id", "") or "").strip()
        if reply_id and not reply_id.startswith("card:"):
            return reply_id
        return ""

    # ---------------- 单次调用

    async def post_message(self, channel: str, *, text: str = "",
                           blocks: list[dict] | None = None,
                           thread_ts: str = "") -> dict:
        if not channel:
            raise SlackApiError("no route: chat_id empty")
        await self._throttle(channel)
        payload: dict[str, Any] = {"channel": channel}
        if blocks:
            payload["blocks"] = list(blocks)[:MAX_BLOCKS]
            payload["text"] = (text or "grape")[:MAX_FALLBACK_TEXT]
        else:
            payload["text"] = (text or "")[:SLACK_TEXT_LIMIT]
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return await self._client.call("chat.postMessage", payload)

    async def update(self, channel: str, ts: str, *, text: str = "",
                     blocks: list[dict] | None = None) -> dict:
        """chat.update：原地更新整卡/整条消息（能力位 MESSAGE_EDIT 的真实实现）。"""
        if not channel or not ts:
            raise SlackApiError("chat.update requires channel and ts")
        await self._throttle(channel)
        payload: dict[str, Any] = {"channel": channel, "ts": ts}
        if blocks:
            payload["blocks"] = list(blocks)[:MAX_BLOCKS]
            payload["text"] = (text or "grape")[:MAX_FALLBACK_TEXT]
        elif (text or "").strip():
            payload["text"] = text[:SLACK_TEXT_LIMIT]
        else:
            raise SlackApiError("chat.update refused empty content")
        return await self._client.call("chat.update", payload)

    async def add_reaction(self, channel: str, ts: str, name: str = "white_check_mark") -> bool:
        """reactions.add：回执语义（能力位 REACTION）。"""
        if not channel or not ts:
            raise SlackApiError("reactions.add requires channel and ts")
        await self._client.call("reactions.add", {
            "channel": channel, "timestamp": ts, "name": _reaction_name(name)})
        return True

    # ---------------- 主入口

    async def send(self, out: OutboundMessage) -> SendResult:
        try:
            md = out.metadata or {}
            raw_card = md.get("raw_card")
            attachments = md.get("attachments") or md.get("files") or []
            text = out.text or ""
            kind = str(md.get("kind") or "")
            if isinstance(raw_card, dict) and (raw_card.get("header") or raw_card.get("elements")):
                # 平台格式泄漏护栏（同钉钉/企微）：lark JSON 不允许透传给 Slack
                return SendResult(ok=False, error="lark raw_card refused on slack")
            if not text.strip() and not raw_card and not attachments:
                return SendResult(ok=False, error="empty content guard")

            channel = out.chat_id or ""
            thread_ts = self.thread_ts_of(out)
            last_id = ""

            blocks: list[dict] = []
            if isinstance(raw_card, dict) and raw_card.get("blocks"):
                blocks = [b for b in raw_card.get("blocks") or [] if isinstance(b, dict)]
            elif kind == "card":
                from .cards import header_block, section_blocks
                title = str(md.get("title") or "DevOps Bot")
                blocks = [header_block(title)] + section_blocks(format_mrkdwn(text))
            if blocks:
                body = await self.post_message(
                    channel, text=text or str(md.get("title") or ""),
                    blocks=blocks, thread_ts=thread_ts)
                last_id = str(body.get("ts") or "")
            elif (text or "").strip():
                chunks = split_text(format_mrkdwn(text))
                for chunk in chunks:
                    body = await self.post_message(channel, text=chunk, thread_ts=thread_ts)
                    last_id = str(body.get("ts") or "") or last_id

            for item in attachments or []:
                if not isinstance(item, dict):
                    continue
                data = item.get("data")
                if data is None and item.get("path"):
                    with open(str(item["path"]), "rb") as f:
                        data = f.read()
                data = bytes(data or b"")
                filename = str(item.get("filename") or item.get("name") or "file.bin")
                file_id = await self._client.upload_file(
                    data, filename, channel=channel, thread_ts=thread_ts,
                    title=str(item.get("title") or ""))
                last_id = file_id or last_id

            return SendResult(ok=True, message_id=last_id)
        except SlackApiError as e:
            log.warning("[slack] send failed: %s", e)
            return SendResult(ok=False, error=str(e))
        except Exception as e:      # 网络/序列化异常不得穿透
            log.warning("[slack] send crashed: %r", e)
            return SendResult(ok=False, error=str(e))


def _reaction_name(emoji: str) -> str:
    raw = str(emoji or "").strip().strip(":")
    if not raw:
        return "white_check_mark"
    if raw in _REACTION_NAMES:
        return _REACTION_NAMES[raw]
    lowered = raw.lower()
    if lowered in _REACTION_NAMES:
        return _REACTION_NAMES[lowered]
    return lowered


# ---------------------------------------------------------------- handler 侧 api

class SlackAdapterApi:
    """装配层 `_api_overrides` 的 Slack api（媒体两跳 + 身份 + 原地更新 + 主动推送）。

    handler 拿到的 image_key/file_key 就是 `url_private_download` 引用（MediaRef
    storage_key），这里按 URL 带 xoxb 下载并缓存；身份显示名走 users.info 缓存。
    更新类操作（chat.update / reactions.add）只给 message ts，channel 从入站台账反查。
    """

    native_interactive_card = True

    def __init__(self, bot_token: str, *,
                 client: SlackClient | None = None,
                 sender: SlackSender | None = None,
                 route_registry: SlackRouteRegistry | None = None,
                 media: SlackMediaFetcher | None = None,
                 users: SlackUserResolver | None = None) -> None:
        self._client = client
        self._sender = sender
        self._routes = route_registry if route_registry is not None else SlackRouteRegistry()
        if media is not None:
            self._media = media
        elif client is not None:
            self._media = SlackMediaFetcher(
                bot_token, http_get=lambda url, _token: client.download(url))
        else:
            self._media = SlackMediaFetcher(bot_token)
        self._users = users if users is not None else SlackUserResolver()

    # ---------------- 媒体 / 身份

    async def download_image(self, ctx: Any, image_key: str, message_id: str = "") -> bytes:
        return await self._download(image_key)

    async def download_file(self, ctx: Any, file_key: str, message_id: str = "",
                            file_type: str = "file") -> bytes:
        return await self._download(file_key)

    async def _download(self, key: str) -> bytes:
        if not key:
            return b""
        try:
            return await self._media.fetch(key)
        except Exception as e:
            log.warning("[slack] media download failed: %r", e)
            return b""

    async def get_user_name(self, ctx: Any, user_id: str, user_id_type: str = "user_id") -> str:
        return await self._users.name(str(user_id or ""))

    # ---------------- 回执 / 原地更新

    async def add_reaction(self, ctx: Any, message_id: str, emoji_type: str = "OK") -> bool:
        channel = self._routes.channel_for(str(message_id or ""))
        if self._sender is None or not channel:
            return False
        try:
            return await self._sender.add_reaction(channel, str(message_id), emoji_type)
        except Exception as e:
            log.warning("[slack] add_reaction failed: %s", e)
            return False

    async def update_card_by_token(self, ctx: Any, action_token: str, card: Any) -> bool:
        """卡片原地更新：token = `<channel>|<ts>`（渲染层卡片的 update token）。"""
        if self._sender is None:
            return False
        channel, ts = parse_card_token(str(action_token or ""))
        blocks = card.get("blocks") if isinstance(card, dict) else None
        text = ""
        if isinstance(card, dict) and not blocks:
            text = str(card.get("text") or "")
        try:
            await self._sender.update(channel, ts, text=text, blocks=blocks)
            return True
        except Exception as e:
            log.warning("[slack] chat.update failed: %s", e)
            return False

    async def update_message(self, ctx: Any, message_id: str, text: str,
                             chat_id: str = "") -> bool:
        channel = str(chat_id or "") or self._routes.channel_for(str(message_id or ""))
        if self._sender is None or not channel:
            return False
        try:
            await self._sender.update(channel, str(message_id), text=format_mrkdwn(text or ""))
            return True
        except Exception as e:
            log.warning("[slack] update_message failed: %s", e)
            return False

    # ---------------- 主动推送（绑定引导卡 / 未绑定卡 / 私聊定向）

    async def send_card_to_chat(self, ctx: Any, chat_id: str, text: str,
                                fallback_open_id: str = "", title: str = "DevOps Bot",
                                template: str = "blue", **kw) -> str:
        if self._sender is None or not chat_id:
            return ""
        out = OutboundMessage(chat_id=chat_id, text=text or "",
                              metadata={"kind": "card", "title": title or "DevOps Bot"})
        try:
            res = await self._sender.send(out)
        except Exception as e:
            log.warning("[slack] send_card_to_chat failed chat=%s: %r", chat_id[-12:], e)
            return ""
        return res.message_id if getattr(res, "ok", False) else ""

    async def send_text_to_user(self, ctx: Any, open_id: str, text: str,
                                chat_id: str = "", **kw) -> str:
        """私聊定向：已知 DM channel 直接用；否则 conversations.open 换 DM 再发。"""
        channel = str(chat_id or "")
        if not channel and open_id and self._client is not None:
            try:
                body = await self._client.call("conversations.open",
                                               {"users": str(open_id)})
                opened = body.get("channel") if isinstance(body.get("channel"), dict) else {}
                channel = str(opened.get("id") or "")
            except Exception as e:
                log.warning("[slack] conversations.open failed: %s", e)
                return ""
        return await self.send_card_to_chat(ctx, channel, text, title="grape")

    async def reply_text(self, ctx: Any, message_id: str, text: str) -> str:
        raise NotImplementedError("legacy egress mode is not supported on slack")

    async def reply_card(self, ctx: Any, message_id: str, text: str, **kw) -> str:
        raise NotImplementedError("legacy egress mode is not supported on slack")


__all__ = [
    "DEFAULT_MIN_INTERVAL",
    "MAX_ACTION_ELEMENTS",
    "SLACK_API_BASE",
    "SLACK_CAPABILITIES",
    "SlackAdapterApi",
    "SlackApiError",
    "SlackClient",
    "SlackSender",
    "card_token",
]
