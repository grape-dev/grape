"""微信入站翻译 —— `WeixinMessage` → `InboundMessage` 信封（Issue #1728 M1）。

真实帧形态（`docs/protocol_zh_CN.md` §3.3）：

    {"msgs": [{"seq": 1, "message_id": 7001, "from_user_id": "u@im.wechat",
               "to_user_id": "bot@im.wechat", "client_id": "...",
               "message_type": 1, "message_state": 2,
               "context_token": "ctx-1", "item_list": [
                    {"type": 1, "text_item": {"text": "hello"}},
                    {"type": 2, "image_item": {"media": {...}, "aeskey": "..."}}],
               "ref_msg": {"message_id": 6988, "item_list": [...]}}],
     "get_updates_buf": "cursor-2"}

防坑：
  1. `message_type=2`（Bot 自己）直接丢弃，避免自回环；
  2. `message_id` 作去重键（`wechat:<bot_id>:<message_id>`）；
  3. `chat_id = from_user_id`（微信单聊），`sender` 同值；
  4. 语音用 `voice_item.text`（微信侧转写）当正文；无转写 → 友好提示文本；
  5. 空内容（无可见项）→ 返回 None，channel 侧计 `inbound_empty`；
  6. 解析异常返回 None（绝不穿透轮询循环）。
"""
from __future__ import annotations

import logging
from typing import Any

from ...channel.message import InboundMessage, MediaRef, ReplyCtx, Source
from ...channel.types import ChatType, MsgType, Type
from .protocol import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_TEXT,
    ITEM_VIDEO,
    ITEM_VOICE,
    MSG_TYPE_BOT,
    as_dict,
    as_list,
    as_str,
    context_token_of,
    extract_text,
    from_user_of,
    item_type,
    message_id_of,
    message_type_of,
    parse_media,
)
from .resolvers import encode_media_key

log = logging.getLogger(__name__)

VOICE_NO_TRANSCRIPT = "（收到一条语音消息，微信侧暂无转写文本，请改用文字描述）"

_MEDIA_TYPE = {
    ITEM_IMAGE: MsgType.IMAGE,
    ITEM_FILE: MsgType.FILE,
    ITEM_VIDEO: MsgType.MEDIA,
    ITEM_VOICE: MsgType.MEDIA,
}


def dedup_key(bot_id: str, message_id: str) -> str:
    return f"wechat:{as_str(bot_id)}:{as_str(message_id)}"


def _ref_text(ref_msg: Any) -> str:
    return extract_text(as_dict(ref_msg).get("item_list"))


def message_to_inbound(msg: Any, *, bot_id: str = "") -> InboundMessage | None:
    """单条 `WeixinMessage` → `InboundMessage`；Bot 自消息/空内容返回 None。"""
    data = as_dict(msg)
    if not data:
        return None
    if message_type_of(data) == MSG_TYPE_BOT:
        return None

    sender = from_user_of(data)
    message_id = message_id_of(data)
    if not sender or not message_id:
        return None

    items = as_list(data.get("item_list"))
    text = extract_text(items)
    media_refs: list[MediaRef] = []
    has_voice_without_text = False
    msg_type = MsgType.TEXT

    for item in items:
        kind = item_type(item)
        if kind == ITEM_TEXT:
            continue
        if kind == ITEM_VOICE:
            voice_text = extract_text([item])
            if voice_text:
                continue
            has_voice_without_text = True
            # 语音即使无转写也保留媒体引用（后续可尝试下载）
        ref_type = _MEDIA_TYPE.get(kind)
        if ref_type is None:
            continue
        media = parse_media(item)
        if not media.get("url"):
            continue
        media_refs.append(MediaRef(
            type=ref_type, storage_key=encode_media_key(media),
            inline_index=len(media_refs)))
        if msg_type == MsgType.TEXT:
            msg_type = ref_type

    if not text and has_voice_without_text:
        text = VOICE_NO_TRANSCRIPT
    if not text and not media_refs:
        return None

    ref_msg = as_dict(data.get("ref_msg"))
    ref_id = as_str(ref_msg.get("message_id"))
    ref_text = _ref_text(ref_msg)
    context_token = context_token_of(data)

    raw = dict(data)
    raw["wechat_bot_id"] = as_str(bot_id)
    raw["context_token"] = context_token
    raw["ref_text"] = ref_text
    raw["sender_name"] = as_str(data.get("sender_name"))

    return InboundMessage(
        event_id=dedup_key(bot_id, message_id),
        message_id=message_id,
        source=Source(
            channel_type=Type.WECHAT,
            chat_id=sender,
            chat_type=ChatType.P2P,
            thread_root="",
            sender_id=sender,
            sender_stable_id=sender,
        ),
        type=msg_type,
        text=text,
        command_text=text,
        media_refs=media_refs,
        reply_to=ReplyCtx(message_id=ref_id or message_id, root_message_id=""),
        addressed_to_bot=True,
        raw=raw,
    )


def messages_to_inbound(msgs: Any, *, bot_id: str = "") -> list[InboundMessage]:
    out: list[InboundMessage] = []
    for item in as_list(msgs):
        msg = message_to_inbound(item, bot_id=bot_id)
        if msg is not None:
            out.append(msg)
    return out


__all__ = [
    "VOICE_NO_TRANSCRIPT",
    "dedup_key",
    "message_to_inbound",
    "messages_to_inbound",
]