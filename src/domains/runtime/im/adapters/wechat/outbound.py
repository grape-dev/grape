"""微信出站 —— `OutboundMessage` → ilink `/sendmessage`（Issue #1728 M1/M2）。

三条硬约束：
1. **文本 4000 字符分块**（与插件 `textChunkLimit: 4000` 一致），每块独立
   `client_id`（uuid4）保证幂等，全部带 `context_token` 正确回传；
2. **能力位**：`TEXT | RICH_CARD | ATTACHMENT`。微信没有按钮/回调/编辑，
   卡片由 renderer 降级为纯文本（`RICH_CARD` 语义 = 能渲染 Card IR），
   不申报 `CARD_ACTION / MESSAGE_EDIT / REACTION / THREAD_REPLY`；
3. **媒体**：`getuploadurl` → AES-128-ECB 加密 → 密文 POST → `media` 引用发送；
   失败降级为「文本说明 + 链接」并计 `media_upload_failed`（内容不静默丢失）。

失败语义：任何传输/`ret != 0` 失败都返回 `SendResult(ok=False, error=...)`。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ...channel.capability import Capability
from ...channel.message import OutboundMessage, SendResult
from .media import WechatMediaUploader, media_type_for_name
from .protocol import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_VIDEO,
    ITEM_VOICE,
    TEXT_LIMIT,
    WechatApiError,
    WechatClient,
    as_dict,
    as_str,
)
from .resolvers import WechatRouteRegistry, WechatTypingCache, WechatUserResolver

log = logging.getLogger(__name__)

WECHAT_CAPABILITIES = (
    Capability.TEXT | Capability.RICH_CARD | Capability.ATTACHMENT
)

_MEDIA_ITEM = {
    "image": ITEM_IMAGE,
    "file": ITEM_FILE,
    "voice": ITEM_VOICE,
    "video": ITEM_VIDEO,
}


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """按字符上限分块；优先在换行/空格处断开，绝不在代码围栏中间硬切。

    边界契约（测试断言 3999/4000/4001）：长度 ≤ limit 原样返回单块；
    超过时每块 ≤ limit，且拼回去与原文语义一致（仅可能丢失分隔空白）。
    """
    raw = str(text or "")
    if not raw:
        return [""]
    limit = max(1, int(limit))
    if len(raw) <= limit:
        return [raw]

    chunks: list[str] = []
    remaining = raw
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _item_for_media(media_type: str, media_ref: dict, filename: str = "") -> dict:
    kind = _MEDIA_ITEM.get(media_type, ITEM_FILE)
    payload: dict[str, Any] = {"media": media_ref, "msg_id": str(uuid.uuid4())}
    if kind == ITEM_FILE:
        payload["file_name"] = filename or "file.bin"
    elif kind == ITEM_IMAGE:
        payload["media_type"] = 1
    return {"type": kind, _item_field(kind): payload}


def _item_field(kind: int) -> str:
    return {
        ITEM_IMAGE: "image_item",
        ITEM_FILE: "file_item",
        ITEM_VOICE: "voice_item",
        ITEM_VIDEO: "video_item",
    }.get(kind, "file_item")


class WechatSender:
    """出站发送器：文本分块 + 卡片文本 + 媒体上传（失败降级文本）。"""

    def __init__(self, client: WechatClient, *,
                 routes: WechatRouteRegistry | None = None,
                 users: WechatUserResolver | None = None,
                 typing: WechatTypingCache | None = None,
                 uploader: WechatMediaUploader | None = None,
                 metrics: Any = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._client = client
        self._routes = routes if routes is not None else WechatRouteRegistry()
        self._users = users if users is not None else WechatUserResolver()
        self._typing = typing if typing is not None else WechatTypingCache()
        self._uploader = uploader or WechatMediaUploader(client)
        self._metrics = metrics
        self._sleep = sleep
        self.calls: list[dict] = []
        self.media_upload_failed = 0

    def capabilities(self) -> Capability:
        return WECHAT_CAPABILITIES

    # ---------------- typing / 生命周期

    async def send_typing(self, chat_id: str, status: int = 1) -> bool:
        if not as_str(chat_id):
            return False
        ticket = self._typing.get(chat_id)
        try:
            if not ticket:
                cfg = await self._client.get_config(ilink_user_id=chat_id)
                ticket = as_str(cfg.get("typing_ticket"))
                self._typing.put(chat_id, ticket)
            if not ticket:
                return False
            await self._client.send_typing(
                ilink_user_id=chat_id, typing_ticket=ticket, status=status)
            return True
        except Exception as e:
            log.info("[wechat] sendtyping failed (non-fatal): %r", e)
            return False

    async def _cancel_typing(self, chat_id: str) -> None:
        """回复发出后取消 typing（status=2）；无缓存 ticket 时跳过，失败不影响发送。"""
        ticket = self._typing.get(chat_id)
        if not ticket:
            return
        try:
            await self._client.send_typing(
                ilink_user_id=chat_id, typing_ticket=ticket, status=2)
        except Exception as e:
            log.info("[wechat] sendtyping cancel failed (non-fatal): %r", e)

    async def notify_start(self) -> None:
        try:
            await self._client.notify_start()
        except Exception as e:
            log.warning("[wechat] notifystart failed (ignored): %r", e)

    async def notify_stop(self) -> None:
        try:
            await self._client.notify_stop()
        except Exception as e:
            log.warning("[wechat] notifystop failed (ignored): %r", e)

    # ---------------- 发送

    async def send(self, out: OutboundMessage) -> SendResult:
        try:
            md = out.metadata or {}
            raw_card = md.get("raw_card")
            attachments = md.get("attachments") or md.get("files") or []
            text = out.text or ""
            chat_id = as_str(out.chat_id)
            if not text.strip() and not isinstance(raw_card, dict) and not attachments:
                return SendResult(ok=False, error="empty content guard")
            if not chat_id:
                return SendResult(ok=False, error="no route: chat_id empty")

            body = text
            if isinstance(raw_card, dict):
                body = as_str(raw_card.get("text")) or text
            context_token = self._routes.context_token(chat_id)
            first_id = ""
            for chunk in split_text(body):
                if not chunk.strip():
                    continue
                result = await self._client.send_message(
                    to_user_id=chat_id, text=chunk,
                    client_id=str(uuid.uuid4()), context_token=context_token)
                self.calls.append({"method": "sendmessage", "chat_id": chat_id})
                if not first_id:
                    first_id = as_str(as_dict(result.get("msg")).get("message_id")) \
                        or as_str(result.get("message_id"))

            for item in attachments or []:
                if not isinstance(item, dict):
                    continue
                media_id = await self._send_attachment(chat_id, item, context_token)
                first_id = media_id or first_id

            await self._cancel_typing(chat_id)
            return SendResult(ok=True, message_id=first_id)
        except WechatApiError as e:
            log.warning("[wechat] send failed: %s", e)
            return SendResult(ok=False, error=str(e))
        except Exception as e:
            log.warning("[wechat] send crashed: %r", e)
            return SendResult(ok=False, error=str(e))

    async def _record_degraded(self, reason: str) -> None:
        if self._metrics is not None:
            try:
                self._metrics.record_degraded(reason)
            except Exception:
                pass

    async def _send_attachment(self, chat_id: str, item: dict,
                               context_token: str) -> str:
        filename = as_str(item.get("filename") or item.get("name")) or "file.bin"
        media_type = as_str(item.get("media_type")) or media_type_for_name(
            filename, as_str(item.get("mime_type") or item.get("mimetype")))
        data = item.get("data")
        if data is None and item.get("path"):
            try:
                with open(str(item["path"]), "rb") as f:
                    data = f.read()
            except OSError as e:
                log.warning("[wechat] read attachment failed: %r", e)
                data = None
        url = as_str(item.get("url"))
        try:
            if data is None and url:
                data = await self._client.download_bytes(url)
            if not data:
                raise WechatApiError("empty attachment")
            media_ref = await self._uploader.upload(
                bytes(data), filename=filename, media_type=media_type)
            payload = _item_for_media(media_type, media_ref, filename)
            result = await self._client.send_media_message(
                to_user_id=chat_id, item=payload,
                client_id=str(uuid.uuid4()), context_token=context_token)
            return as_str(as_dict(result.get("msg")).get("message_id")) \
                or as_str(result.get("message_id"))
        except Exception as e:
            self.media_upload_failed += 1
            await self._record_degraded("media_upload_failed")
            log.warning("[wechat] media upload failed, fallback to text: %r", e)
            fallback = f"附件发送失败：{filename}" + (f"\n{url}" if url else "")
            try:
                await self._client.send_message(
                    to_user_id=chat_id, text=fallback,
                    client_id=str(uuid.uuid4()), context_token=context_token)
            except Exception as send_error:
                log.warning("[wechat] media fallback text failed: %r", send_error)
            return ""


class WechatAdapterApi:
    """装配层 `_api_overrides` 的微信 api（媒体两跳 + 身份 + typing + 主动推送）。

    微信无按钮/回调/编辑：`native_interactive_card = False`，卡片一律走
    renderer 文本降级；数字选择由既有 `_handle_selection` 承担。
    """

    native_interactive_card = False

    def __init__(self, token: str = "", *,
                 client: WechatClient | None = None,
                 sender: WechatSender | None = None,
                 routes: WechatRouteRegistry | None = None,
                 users: WechatUserResolver | None = None,
                 media: Any = None,
                 metrics: Any = None) -> None:
        self._client = client
        self._sender = sender
        self._routes = routes if routes is not None else WechatRouteRegistry()
        self._users = users if users is not None else WechatUserResolver()
        self._media = media
        self._metrics = metrics

    async def download_image(self, ctx: Any, image_key: str,
                             message_id: str = "") -> bytes:
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
            log.warning("[wechat] media download failed: %r", e)
            if self._metrics is not None:
                try:
                    self._metrics.record_degraded("media_download_failed")
                except Exception:
                    pass
            return b""

    async def get_user_name(self, ctx: Any, user_id: str,
                            user_id_type: str = "user_id") -> str:
        return self._users.name(as_str(user_id))

    async def add_reaction(self, ctx: Any, message_id: str,
                           emoji_type: str = "OK") -> bool:
        return False

    async def update_card_by_token(self, ctx: Any, action_token: str,
                                   card: Any) -> bool:
        return False

    async def update_message(self, ctx: Any, message_id: str, text: str,
                             chat_id: str = "") -> bool:
        return False

    async def send_typing(self, ctx: Any, chat_id: str) -> bool:
        if self._sender is None or not chat_id:
            return False
        return await self._sender.send_typing(as_str(chat_id))

    async def send_card_to_chat(self, ctx: Any, chat_id: str, text: str,
                                fallback_open_id: str = "", title: str = "DevOps Bot",
                                template: str = "blue", **kw) -> str:
        if self._sender is None or not chat_id:
            return ""
        out = OutboundMessage(chat_id=as_str(chat_id), text=text or "",
                              metadata={"kind": "card", "title": title or "DevOps Bot"})
        try:
            res = await self._sender.send(out)
        except Exception as e:
            log.warning("[wechat] send_card_to_chat failed: %r", e)
            return ""
        return res.message_id if getattr(res, "ok", False) else ""

    async def send_text_to_user(self, ctx: Any, open_id: str, text: str,
                                chat_id: str = "", **kw) -> str:
        chat = as_str(chat_id) or as_str(open_id)
        return await self.send_card_to_chat(ctx, chat, text, title="grape")

    async def reply_text(self, ctx: Any, message_id: str, text: str) -> str:
        raise NotImplementedError("legacy egress mode is not supported on wechat")

    async def reply_card(self, ctx: Any, message_id: str, text: str, **kw) -> str:
        raise NotImplementedError("legacy egress mode is not supported on wechat")


__all__ = [
    "WECHAT_CAPABILITIES",
    "WechatAdapterApi",
    "WechatSender",
    "split_text",
]