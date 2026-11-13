"""Telegram 出站 —— OutboundMessage → Bot HTTP API（Issue #1613 §3.3 / §4）。

三条硬约束：
1. **出站全走 `https://api.telegram.org/bot<token>/<method>`**；单条 text 硬上限 4096
   字符 → markdown.py 按 3900 分块；HTML 解析失败回退纯文本（计 card_text_fallback）。
2. **限流**：全局约 30 msg/s、单聊/单群约 1 msg/s；超限回 `429` + `parameters.retry_after`
   → 客户端按 retry_after 退避（≤3 次），发送器按 chat 节流。
3. **媒体**：出站 `sendPhoto` / `sendDocument`（50MB 上限）；URL 直传公开链接，
   本地/内存文件走 multipart。

卡片：`metadata.raw_card = {"text": ..., "reply_markup": {...}}` 直接发；
`editMessageText` / `editMessageReplyMarkup` 原地更新；`setMessageReaction` 回执。

失败语义（继承 #1008）：任何传输/`ok=false` 失败都返回 SendResult(ok=False, error=...)，
不静默计成功；空内容硬护栏与 `card_empty_guard` 同口径。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ...channel.capability import Capability
from ...channel.message import OutboundMessage, SendResult
from ...channel.types import MsgType
from .markdown import TELEGRAM_TEXT_LIMIT, format_html, split_text
from .resolvers import (
    TelegramMediaFetcher,
    TelegramRouteRegistry,
    TelegramUserResolver,
)

log = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = 20.0
LONG_POLL_TIMEOUT = 25.0
MAX_RATE_LIMIT_RETRIES = 3
DEFAULT_MIN_INTERVAL = 1.0
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_UPDATES = ("message", "edited_message", "callback_query", "my_chat_member")

TELEGRAM_CAPABILITIES = (
    Capability.TEXT | Capability.RICH_CARD | Capability.CARD_ACTION
    | Capability.ATTACHMENT | Capability.MESSAGE_EDIT | Capability.REACTION
    | Capability.THREAD_REPLY | Capability.QUOTE_REPLY | Capability.TYPING_INDICATOR
)

_REACTION_EMOJI = {
    "OK": "👍", "ok": "👍", "OK_HAND": "👌", "THUMBSUP": "👍",
    "THUMBS_UP": "👍", "EYES": "👀", "THINKING": "🤔", "DONE": "✅",
}


class TelegramApiError(RuntimeError):
    """Telegram Bot API 失败（传输 / ok=false / 限流重试耗尽）。"""

    def __init__(self, message: str, *, code: int = 0,
                 description: str = "") -> None:
        super().__init__(message)
        self.code = int(code or 0)
        self.description = description or str(message)


# ---------------------------------------------------------------- HTTP 传输

async def _default_http_post(url: str, payload: dict, timeout: float
                             ) -> tuple[int, dict, dict]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}
    return resp.status_code, dict(resp.headers), (body if isinstance(body, dict) else {})


async def _default_http_get(url: str, timeout: float) -> bytes:
    import httpx
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise TelegramApiError(f"file download HTTP {resp.status_code}")
    return bytes(resp.content or b"")


async def _default_http_multipart(url: str, fields: dict, file_field: str,
                                  filename: str, data: bytes,
                                  timeout: float) -> tuple[int, dict, dict]:
    import httpx
    files = {file_field: (filename or "file.bin", data, "application/octet-stream")}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, data=fields, files=files)
    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}
    return resp.status_code, dict(resp.headers), (body if isinstance(body, dict) else {})


def _retry_after_seconds(headers: dict, body: dict) -> float:
    raw = None
    if isinstance(body, dict):
        params = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
        raw = params.get("retry_after")
    if raw is None:
        raw = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        value = float(raw) if raw is not None else 1.0
    except Exception:
        value = 1.0
    return max(0.0, min(value, 60.0))


class TelegramClient:
    """Bot API 客户端：token 鉴权 + 429 退避 + 文件下载/上传。

    注入点：http_post / http_get / http_multipart / sleep —— 单测全程不触网。
    """

    def __init__(self, token: str, *,
                 base_url: str = TELEGRAM_API_BASE,
                 http_post: Callable[..., Awaitable[tuple[int, dict, dict]]] | None = None,
                 http_get: Callable[[str, float], Awaitable[bytes]] | None = None,
                 http_multipart: Callable[..., Awaitable[tuple[int, dict, dict]]] | None = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = MAX_RATE_LIMIT_RETRIES,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._token = str(token or "")
        self._base_url = str(base_url or TELEGRAM_API_BASE).rstrip("/")
        self._http_post = http_post or _default_http_post
        self._http_get = http_get or _default_http_get
        self._http_multipart = http_multipart or _default_http_multipart
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._sleep = sleep
        self.calls: list[dict] = []
        self.rate_limited = 0

    @property
    def token(self) -> str:
        return self._token

    def _url(self, method: str) -> str:
        return f"{self._base_url}/bot{self._token}/{method}"

    async def call(self, method: str, payload: dict | None = None, *,
                   timeout: float | None = None) -> Any:
        """POST /bot<token>/<method>：ok=false 抛 TelegramApiError；429 按 retry_after 退避。"""
        body_in = payload or {}
        attempt = 0
        while True:
            status, headers, body = await self._http_post(
                self._url(method), body_in, timeout or self._timeout)
            self.calls.append({"method": method, "payload": _redacted_payload(body_in),
                               "status": status})
            if status == 429 or (isinstance(body, dict) and body.get("error_code") == 429):
                self.rate_limited += 1
                if attempt >= self._max_retries:
                    raise TelegramApiError(
                        f"{method} rate limited after {attempt + 1} attempts", code=429)
                delay = _retry_after_seconds(headers, body if isinstance(body, dict) else {})
                log.info("[telegram] %s 429, retry in %.1fs (attempt %s)",
                         method, delay, attempt + 1)
                await self._sleep(delay)
                attempt += 1
                continue
            if not isinstance(body, dict):
                raise TelegramApiError(f"{method} returned non-object body", code=status)
            if status >= 400 or not body.get("ok"):
                code = int(body.get("error_code") or status or 0)
                desc = str(body.get("description") or "")
                raise TelegramApiError(
                    f"{method} failed: {desc or 'unknown_error'}", code=code, description=desc)
            return body.get("result")

    async def get_updates(self, offset: int, *,
                          timeout: float = LONG_POLL_TIMEOUT,
                          allowed_updates: tuple[str, ...] = ALLOWED_UPDATES) -> list[dict]:
        result = await self.call("getUpdates", {
            "offset": int(offset or 0),
            "timeout": int(timeout),
            "allowed_updates": list(allowed_updates),
        }, timeout=timeout + 10.0)
        return [item for item in (result or []) if isinstance(item, dict)]

    async def get_me(self) -> dict:
        result = await self.call("getMe")
        return result if isinstance(result, dict) else {}

    async def get_file(self, file_id: str) -> dict:
        result = await self.call("getFile", {"file_id": str(file_id or "")})
        return result if isinstance(result, dict) else {}

    async def download_file(self, file_path: str) -> bytes:
        url = f"{self._base_url}/file/bot{self._token}/{file_path}"
        return bytes(await self._http_get(url, self._timeout) or b"")

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        payload: dict[str, Any] = {"callback_query_id": str(callback_query_id or "")}
        if text:
            payload["text"] = text[:200]
        await self.call("answerCallbackQuery", payload)
        return True

    async def send_media(self, method: str, chat_id: str, *,
                         data: bytes | None = None, filename: str = "file.bin",
                         url: str = "", caption: str = "",
                         reply_to_message_id: str = "") -> dict:
        if url:
            # Bot API sendPhoto/sendDocument 只接受 photo/document 键（URL 或 file_id），
            # 旧实现写成 file_id 会被 400 拒绝。
            field_name = "photo" if method == "sendPhoto" else "document"
            payload: dict[str, Any] = {"chat_id": str(chat_id), field_name: url}
            if caption:
                payload["caption"] = caption[:1024]
            if reply_to_message_id:
                payload["reply_to_message_id"] = _as_int(reply_to_message_id)
            return await self.call(method, payload)
        if not data:
            raise TelegramApiError("empty media payload")
        if len(data) > MAX_UPLOAD_BYTES:
            raise TelegramApiError("media exceeds 50MB upload limit")
        fields: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption[:1024]
        if reply_to_message_id:
            fields["reply_to_message_id"] = _as_int(reply_to_message_id)
        field_name = "photo" if method == "sendPhoto" else "document"
        attempt = 0
        while True:
            status, headers, body = await self._http_multipart(
                self._url(method), fields, field_name, filename, data, self._timeout)
            self.calls.append({"method": method, "payload": {"chat_id": str(chat_id)},
                               "status": status})
            if status == 429 or (isinstance(body, dict) and body.get("error_code") == 429):
                # 上传遇 429 必须按 retry_after 用同一份 multipart 重试；旧实现退回
                # self.call(method, fields) 会退化成不含文件的 JSON 请求，重试必然失败。
                self.rate_limited += 1
                if attempt >= self._max_retries:
                    raise TelegramApiError(
                        f"{method} rate limited after {attempt + 1} attempts", code=429)
                delay = _retry_after_seconds(
                    headers, body if isinstance(body, dict) else {})
                log.info("[telegram] %s 429 (multipart), retry in %.1fs (attempt %s)",
                         method, delay, attempt + 1)
                await self._sleep(delay)
                attempt += 1
                continue
            if status >= 400 or not isinstance(body, dict) or not body.get("ok"):
                desc = str((body or {}).get("description") or "")
                raise TelegramApiError(
                    f"{method} failed: {desc or 'unknown_error'}", code=status,
                    description=desc)
            result = body.get("result")
            return result if isinstance(result, dict) else {}


def _as_int(value: Any) -> Any:
    try:
        return int(value)
    except Exception:
        return value


def _redacted_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    redacted = dict(payload)
    for key in ("token", "bot_token"):
        if key in redacted:
            redacted[key] = "***"
    return redacted


# ---------------------------------------------------------------- Sender

class TelegramSender:
    """出站发送器：文本（3900 分块 / HTML 回退）+ 卡片 + 更新/表情/媒体。"""

    def __init__(self, client: TelegramClient, *,
                 min_interval: float = DEFAULT_MIN_INTERVAL,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._client = client
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._last_sent: dict[str, float] = {}
        self.text_fallback = 0
        self.calls: list[dict] = []

    def capabilities(self) -> Capability:
        return TELEGRAM_CAPABILITIES

    async def _throttle(self, chat_id: str) -> None:
        if self._min_interval <= 0:
            self._last_sent[chat_id] = self._clock()
            return
        last = self._last_sent.get(chat_id)
        now = self._clock()
        if last is not None:
            wait = self._min_interval - (now - last)
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
        self._last_sent[chat_id] = now

    async def send_message(self, chat_id: str, text: str, *,
                           parse_mode: str = "HTML",
                           reply_markup: dict | None = None,
                           reply_to_message_id: str = "") -> dict:
        if not chat_id:
            raise TelegramApiError("no route: chat_id empty")
        await self._throttle(chat_id)
        payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_to_message_id"] = _as_int(reply_to_message_id)
        self.calls.append({"method": "sendMessage", "payload": payload})
        return await self._client.call("sendMessage", payload)

    async def send_text(self, chat_id: str, text: str, *,
                        reply_to_message_id: str = "",
                        reply_markup: dict | None = None) -> dict:
        """发送标准 Markdown 文本：分块 + HTML，解析失败回退纯文本。"""
        last: dict = {}
        for chunk in split_text(text or ""):
            html = format_html(chunk)
            try:
                last = await self.send_message(
                    chat_id, html, parse_mode="HTML", reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id)
            except TelegramApiError as e:
                if not self._is_parse_error(e):
                    raise
                self.text_fallback += 1
                last = await self.send_message(
                    chat_id, chunk, parse_mode="", reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id)
        return last

    @staticmethod
    def _is_parse_error(error: TelegramApiError) -> bool:
        text = (str(error.description or error) + " " + str(error)).lower()
        return "parse" in text or "entities" in text

    async def edit_message_text(self, chat_id: str, message_id: str, *,
                                text: str = "", parse_mode: str = "HTML",
                                reply_markup: dict | None = None) -> dict:
        if not chat_id or not message_id:
            raise TelegramApiError("editMessageText requires chat_id and message_id")
        await self._throttle(chat_id)
        payload: dict[str, Any] = {
            "chat_id": str(chat_id), "message_id": _as_int(message_id), "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.calls.append({"method": "editMessageText", "payload": payload})
        try:
            return await self._client.call("editMessageText", payload)
        except TelegramApiError as e:
            if not self._is_parse_error(e):
                raise
            payload.pop("parse_mode", None)
            self.text_fallback += 1
            return await self._client.call("editMessageText", payload)

    async def edit_message_reply_markup(self, chat_id: str, message_id: str,
                                        reply_markup: dict) -> dict:
        await self._throttle(chat_id)
        return await self._client.call("editMessageReplyMarkup", {
            "chat_id": str(chat_id), "message_id": _as_int(message_id),
            "reply_markup": reply_markup,
        })

    async def add_reaction(self, chat_id: str, message_id: str, emoji: str = "OK") -> bool:
        if not chat_id or not message_id:
            raise TelegramApiError("setMessageReaction requires chat_id and message_id")
        await self._client.call("setMessageReaction", {
            "chat_id": str(chat_id), "message_id": _as_int(message_id),
            "reaction": [{"type": "emoji", "emoji": _reaction_emoji(emoji)}],
        })
        return True

    async def send_typing(self, chat_id: str) -> bool:
        await self._client.call("sendChatAction", {
            "chat_id": str(chat_id), "action": "typing"})
        return True

    # ---------------- 主入口

    async def send(self, out: OutboundMessage) -> SendResult:
        try:
            md = out.metadata or {}
            raw_card = md.get("raw_card")
            attachments = md.get("attachments") or md.get("files") or []
            text = out.text or ""
            chat_id = out.chat_id or ""
            reply_id = str(getattr(out.reply_to, "message_id", "") or "")
            if not text.strip() and not raw_card and not attachments:
                return SendResult(ok=False, error="empty content guard")

            last_id = ""
            if isinstance(raw_card, dict):
                card_text = str(raw_card.get("text") or text or "")
                reply_markup = raw_card.get("reply_markup")
                parse_mode = str(raw_card.get("parse_mode") or "HTML")
                result = await self.send_card(
                    chat_id, card_text, parse_mode=parse_mode,
                    reply_markup=reply_markup if isinstance(reply_markup, dict) else None,
                    reply_to_message_id=reply_id)
                last_id = str((result or {}).get("message_id") or "")
            elif text.strip():
                result = await self.send_text(
                    chat_id, text, reply_to_message_id=reply_id)
                last_id = str((result or {}).get("message_id") or "")

            for item in attachments or []:
                if not isinstance(item, dict):
                    continue
                media_id = await self._send_attachment(chat_id, item, reply_id)
                last_id = media_id or last_id

            return SendResult(ok=True, message_id=last_id)
        except TelegramApiError as e:
            log.warning("[telegram] send failed: %s", e)
            return SendResult(ok=False, error=str(e))
        except Exception as e:
            log.warning("[telegram] send crashed: %r", e)
            return SendResult(ok=False, error=str(e))

    async def send_card(self, chat_id: str, text: str, *,
                        parse_mode: str = "HTML",
                        reply_markup: dict | None = None,
                        reply_to_message_id: str = "") -> dict:
        try:
            return await self.send_message(
                chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id)
        except TelegramApiError as e:
            if not self._is_parse_error(e):
                raise
            self.text_fallback += 1
            return await self.send_message(
                chat_id, text, parse_mode="", reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id)

    async def _send_attachment(self, chat_id: str, item: dict,
                               reply_id: str) -> str:
        filename = str(item.get("filename") or item.get("name") or "file.bin")
        url = str(item.get("url") or item.get("file_id") or "")
        mime = str(item.get("mime_type") or item.get("mimetype") or "").lower()
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        is_photo = (mime.startswith("image/")
                    or ext in {"png", "jpg", "jpeg", "gif", "webp"})
        method = "sendPhoto" if is_photo else "sendDocument"
        if url and not (item.get("data") or item.get("path")):
            result = await self._client.send_media(
                method, chat_id, url=url, caption=str(item.get("caption") or ""),
                reply_to_message_id=reply_id)
            return str((result or {}).get("message_id") or "")
        data = item.get("data")
        if data is None and item.get("path"):
            with open(str(item["path"]), "rb") as f:
                data = f.read()
        data = bytes(data or b"")
        result = await self._client.send_media(
            method, chat_id, data=data, filename=filename,
            caption=str(item.get("caption") or ""),
            reply_to_message_id=reply_id)
        return str((result or {}).get("message_id") or "")


def _reaction_emoji(emoji: str) -> str:
    raw = str(emoji or "").strip()
    if not raw:
        return "👍"
    return _REACTION_EMOJI.get(raw) or _REACTION_EMOJI.get(raw.upper()) or raw


# ---------------------------------------------------------------- handler 侧 api

class TelegramAdapterApi:
    """装配层 `_api_overrides` 的 Telegram api（媒体两跳 + 身份 + 原地更新 + 主动推送）。"""

    native_interactive_card = True

    def __init__(self, token: str, *,
                 client: TelegramClient | None = None,
                 sender: TelegramSender | None = None,
                 route_registry: TelegramRouteRegistry | None = None,
                 media: TelegramMediaFetcher | None = None,
                 users: TelegramUserResolver | None = None) -> None:
        self._client = client
        self._sender = sender
        self._routes = route_registry if route_registry is not None else TelegramRouteRegistry()
        if media is not None:
            self._media = media
        elif client is not None:
            self._media = TelegramMediaFetcher(client)
        else:
            self._media = None
        self._users = users if users is not None else TelegramUserResolver()

    # ---------------- 媒体 / 身份

    async def download_image(self, ctx: Any, image_key: str, message_id: str = "") -> bytes:
        return await self._download(image_key)

    async def download_file(self, ctx: Any, file_key: str, message_id: str = "",
                            file_type: str = "file") -> bytes:
        return await self._download(file_key)

    async def _download(self, key: str) -> bytes:
        if not key or self._media is None:
            return b""
        try:
            return await self._media.fetch(key)
        except Exception as e:
            log.warning("[telegram] media download failed: %r", e)
            return b""

    async def get_user_name(self, ctx: Any, user_id: str,
                            user_id_type: str = "user_id") -> str:
        return self._users.name(str(user_id or ""))

    # ---------------- 回执 / 原地更新

    async def add_reaction(self, ctx: Any, message_id: str,
                           emoji_type: str = "OK") -> bool:
        chat_id = self._routes.channel_for(str(message_id or ""))
        if self._sender is None or not chat_id:
            return False
        try:
            return await self._sender.add_reaction(chat_id, str(message_id), emoji_type)
        except Exception as e:
            log.warning("[telegram] add_reaction failed: %s", e)
            return False

    async def update_card_by_token(self, ctx: Any, action_token: str,
                                   card: Any) -> bool:
        """卡片原地更新：token = `<chat_id>|<message_id>`。"""
        if self._sender is None:
            return False
        chat_id, _, message_id = str(action_token or "").partition("|")
        if not chat_id or not message_id:
            return False
        text = str(card.get("text") or "") if isinstance(card, dict) else ""
        reply_markup = card.get("reply_markup") if isinstance(card, dict) else None
        try:
            await self._sender.edit_message_text(
                chat_id, message_id, text=text,
                reply_markup=reply_markup if isinstance(reply_markup, dict) else None)
            return True
        except Exception as e:
            log.warning("[telegram] editMessageText failed: %s", e)
            return False

    async def update_message(self, ctx: Any, message_id: str, text: str,
                             chat_id: str = "") -> bool:
        chat = str(chat_id or "") or self._routes.channel_for(str(message_id or ""))
        if self._sender is None or not chat:
            return False
        try:
            await self._sender.edit_message_text(
                chat, str(message_id), text=format_html(text or ""))
            return True
        except Exception as e:
            log.warning("[telegram] update_message failed: %s", e)
            return False

    async def send_typing(self, ctx: Any, chat_id: str) -> bool:
        if self._sender is None or not chat_id:
            return False
        try:
            return await self._sender.send_typing(str(chat_id))
        except Exception as e:
            log.warning("[telegram] sendChatAction failed: %s", e)
            return False

    # ---------------- 主动推送

    async def send_card_to_chat(self, ctx: Any, chat_id: str, text: str,
                                fallback_open_id: str = "", title: str = "DevOps Bot",
                                template: str = "blue", **kw) -> str:
        if self._sender is None or not chat_id:
            return ""
        out = OutboundMessage(chat_id=str(chat_id), text=text or "",
                              metadata={"kind": "card", "title": title or "DevOps Bot"})
        try:
            res = await self._sender.send(out)
        except Exception as e:
            log.warning("[telegram] send_card_to_chat failed chat=%s: %r",
                        str(chat_id)[-12:], e)
            return ""
        return res.message_id if getattr(res, "ok", False) else ""

    async def send_text_to_user(self, ctx: Any, open_id: str, text: str,
                                chat_id: str = "", **kw) -> str:
        chat = str(chat_id or "") or str(open_id or "")
        return await self.send_card_to_chat(ctx, chat, text, title="grape")

    async def reply_text(self, ctx: Any, message_id: str, text: str) -> str:
        raise NotImplementedError("legacy egress mode is not supported on telegram")

    async def reply_card(self, ctx: Any, message_id: str, text: str, **kw) -> str:
        raise NotImplementedError("legacy egress mode is not supported on telegram")


__all__ = [
    "ALLOWED_UPDATES",
    "DEFAULT_MIN_INTERVAL",
    "LONG_POLL_TIMEOUT",
    "MAX_UPLOAD_BYTES",
    "TELEGRAM_API_BASE",
    "TELEGRAM_CAPABILITIES",
    "TelegramAdapterApi",
    "TelegramApiError",
    "TelegramClient",
    "TelegramSender",
]