"""Telegram 入站翻译 —— Update / callback_query → InboundMessage 信封（Issue #1613）。

真实 Update 形态（Bot API）：

    {"update_id": 100, "message": {"message_id": 5, "date": ...,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 42, "first_name": "Alice"},
        "text": "hello", "reply_to_message": {...}, "message_thread_id": 9,
        "photo": [{"file_id": "small", "file_size": 100}, {"file_id": "big", ...}],
        "document": {"file_id": "...", "file_name": "a.pdf", "file_size": 1024}}}

    {"update_id": 101, "callback_query": {"id": "cq-1", "from": {"id": 42},
        "data": "b:2", "message": {"message_id": 7, "chat": {"id": 123}}}}

防坑（设计 §3.2）：
  1. `edited_message` 显式忽略（channel 侧推进 offset 但不派发），防同一条被当新消息；
  2. 群消息剥 `@botname`；剥离后为空 → 未 @ 机器人（引擎群过滤丢弃）；
  3. 媒体只存 `file_id` 引用（两跳纪律），`file_size` 超 20MB 标记超限（channel 回执）；
  4. 解析异常返回 None（绝不穿透接收循环）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ...channel.message import InboundMessage, MediaRef, ReplyCtx, Source
from ...channel.types import ChatType, MsgType, Type
from .cards import TelegramCardLedger, decode_callback_data

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_CAPTION_TEXT = 4096

_IMAGE_MIME_PREFIX = "image/"
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "svg"})


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def chat_type_of(chat: Any) -> ChatType:
    kind = str(_as_dict(chat).get("type") or "")
    return ChatType.P2P if kind == "private" else ChatType.GROUP


def strip_bot_mention(text: str, bot_username: str = "") -> tuple[str, bool]:
    """剥离 `@botname`（Telegram 群内 @ 提及）；返回 (正文, 是否提及机器人)。"""
    raw = text or ""
    if bot_username:
        pattern = re.compile(r"@" + re.escape(bot_username.lstrip("@")) + r"\b",
                             re.IGNORECASE)
        mentioned = bool(pattern.search(raw))
        cleaned = pattern.sub("", raw)
    else:
        # 未知 bot 用户名：只剥离首部 @token，避免误删用户正文中的邮箱/提及
        pattern = re.compile(r"^@[A-Za-z0-9_]{1,64}\s*")
        mentioned = bool(pattern.match(raw))
        cleaned = pattern.sub("", raw)
    cleaned = re.sub(r"[ \t\u00a0]{2,}", " ", cleaned).strip()
    return cleaned, mentioned


def _file_ext(item: dict) -> str:
    name = str(item.get("file_name") or "")
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def media_from_message(message: dict) -> tuple[list[MediaRef], MsgType, bool]:
    """message 媒体字段 → (MediaRef 列表, 消息主类型, 是否超限)。"""
    refs: list[MediaRef] = []
    msg_type = MsgType.TEXT
    too_large = False

    def _add(file_id: str, ref_type: MsgType, index: int, size: Any) -> None:
        nonlocal too_large, msg_type
        if not file_id:
            return
        try:
            if int(size or 0) > MAX_DOWNLOAD_BYTES:
                too_large = True
        except Exception:
            pass
        refs.append(MediaRef(type=ref_type, storage_key=str(file_id), inline_index=index))
        if msg_type == MsgType.TEXT:
            msg_type = ref_type

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        best = photos[-1] if isinstance(photos[-1], dict) else {}
        _add(str(best.get("file_id") or ""), MsgType.IMAGE, 0, best.get("file_size"))

    document = _as_dict(message.get("document"))
    if document.get("file_id"):
        mimetype = str(document.get("mime_type") or "").lower()
        ext = _file_ext(document)
        ref_type = (MsgType.IMAGE
                    if mimetype.startswith(_IMAGE_MIME_PREFIX) or ext in _IMAGE_EXTS
                    else MsgType.FILE)
        _add(str(document.get("file_id") or ""), ref_type, len(refs),
             document.get("file_size"))

    for key, ref_type in (("video", MsgType.MEDIA), ("animation", MsgType.MEDIA),
                          ("voice", MsgType.MEDIA), ("audio", MsgType.MEDIA)):
        item = _as_dict(message.get(key))
        if item.get("file_id"):
            _add(str(item.get("file_id") or ""), ref_type, len(refs),
                 item.get("file_size"))

    return refs, (msg_type if refs else MsgType.TEXT), too_large


def update_to_inbound(update: Any, bot_username: str = "") -> InboundMessage | None:
    """Update（message 分支）→ InboundMessage；不可翻译返回 None。"""
    data = _as_dict(update)
    message = _as_dict(data.get("message"))
    if not message:
        return None

    chat = _as_dict(message.get("chat"))
    chat_id = str(chat.get("id") or "")
    message_id = str(message.get("message_id") or "")
    if not chat_id or not message_id:
        return None

    sender = _as_dict(message.get("from"))
    sender_id = str(sender.get("id") or "")
    chat_type = chat_type_of(chat)

    raw_text = str(message.get("text") or message.get("caption") or "")[:MAX_CAPTION_TEXT]
    text, mentioned = strip_bot_mention(raw_text, bot_username)
    media_refs, msg_type, too_large = media_from_message(message)
    if not text and not media_refs:
        return None

    if chat_type == ChatType.P2P:
        addressed = True
    else:
        # privacy mode：群内只有 /cmd 与 @bot 可见
        addressed = mentioned or text.startswith("/")

    thread_root = str(message.get("message_thread_id") or "")
    reply_to_message = _as_dict(message.get("reply_to_message"))
    reply_id = str(reply_to_message.get("message_id") or "")

    raw = dict(data)
    if too_large:
        raw["media_too_large"] = True
    raw["sender_name"] = " ".join(
        part for part in (str(sender.get("first_name") or ""),
                          str(sender.get("last_name") or "")) if part)

    return InboundMessage(
        event_id=str(data.get("update_id") or message_id),
        message_id=message_id,
        source=Source(
            channel_type=Type.TELEGRAM,
            chat_id=chat_id,
            chat_type=chat_type,
            thread_root=thread_root,
            sender_id=sender_id,
            sender_stable_id=sender_id,
        ),
        type=msg_type,
        text=text,
        command_text=text,
        media_refs=media_refs,
        reply_to=ReplyCtx(message_id=reply_id or message_id, root_message_id=thread_root),
        addressed_to_bot=addressed,
        raw=raw,
    )


def callback_query_to_inbound(callback: Any,
                              ledger: TelegramCardLedger | None = None) -> InboundMessage | None:
    """callback_query → InboundMessage（按钮点击变成一条"用户命令"）。

    `data` 是我们渲染时写入的短码（`b:2` / `c:<cid>`）；解码后 `text` 就是
    `"/bind 2"`，与"回数字"走同一份 `_handle_selection`。

    `raw` 额外带：
      - `action_value`：载荷字典（命令链路读它）；
      - `token`：`<chat_id>|<message_id>`，供 editMessageText 原地更新；
      - `callback_query_id`：ack 用。
    """
    data = _as_dict(callback)
    payload = decode_callback_data(str(data.get("data") or ""), ledger)
    command = str(payload.get("cmd") or "").strip()
    if not command:
        return None
    message = _as_dict(data.get("message"))
    chat = _as_dict(message.get("chat"))
    chat_id = str(chat.get("id") or "")
    message_id = str(message.get("message_id") or "")
    if not chat_id or not message_id:
        return None
    sender = _as_dict(data.get("from"))
    sender_id = str(sender.get("id") or "")
    thread_root = str(message.get("message_thread_id") or "")
    cq_id = str(data.get("id") or "")

    raw = dict(data)
    raw["action_value"] = payload
    raw["token"] = f"{chat_id}|{message_id}"
    raw["callback_query_id"] = cq_id
    raw["card_message_id"] = message_id
    raw["sender_name"] = " ".join(
        part for part in (str(sender.get("first_name") or ""),
                          str(sender.get("last_name") or "")) if part)

    return InboundMessage(
        event_id=f"cb:{cq_id or chat_id + ':' + message_id}",
        message_id=message_id,
        source=Source(
            channel_type=Type.TELEGRAM,
            chat_id=chat_id,
            chat_type=chat_type_of(chat),
            thread_root=thread_root,
            sender_id=sender_id,
            sender_stable_id=sender_id,
        ),
        type=MsgType.CARD_ACTION,
        text=command,
        command_text=command,
        reply_to=ReplyCtx(message_id=message_id, root_message_id=thread_root),
        addressed_to_bot=True,
        raw=raw,
    )


__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "callback_query_to_inbound",
    "chat_type_of",
    "media_from_message",
    "strip_bot_mention",
    "update_to_inbound",
]