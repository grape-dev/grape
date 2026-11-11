"""飞书入站翻译 —— parsed dict（_parse_event 契约）→ InboundMessage 信封。

P0 双写期：旧 handler 照常处理，本翻译同步产出 InboundMessage 落库对比
（§8 P0「双写 7 天零 diff」）。含 card.action.trigger 事件翻译
（原 feishu_async.py L911-913 _parse_card_action 语义迁移）。
"""
from __future__ import annotations

from typing import Any, Optional

from ...channel.message import (
    InboundMessage, MediaRef, ReplyCtx, Source,
)
from ...channel.types import ChatType, MsgType, Type


def _chat_type_of(v: str) -> ChatType:
    return ChatType.GROUP if v == "group" else ChatType.P2P


def _msg_type_of(v: str) -> MsgType:
    return {
        "text": MsgType.TEXT,
        "image": MsgType.IMAGE,
        "file": MsgType.FILE,
        "media": MsgType.MEDIA,
    }.get(v, MsgType.UNKNOWN)


def parse_card_action(raw_event: dict) -> Optional[dict]:
    """card.action.trigger 事件 → parsed dict（与 legacy _parse_card_action 同语义）。

    关键：卡片回调没有真实 message_id —— 去重用 header.event_id，**回复**必须用
    context.open_message_id（_reply_id），否则飞书报 99992354 invalid open_message_id。
    另带 action_value（按钮上下文）与 token（卡片原地更新令牌）。
    """
    event = raw_event.get("event") or {}
    header = raw_event.get("header") or {}
    action = event.get("action") or {}
    value = action.get("value") or {}
    if not value:
        return None
    ctx_ = event.get("context") or {}
    real_msg_id = str(ctx_.get("open_message_id") or "")
    return {
        "action_value": value,
        "chat_id": event.get("chat_id", "") or str(ctx_.get("open_chat_id") or ""),
        "open_id": (event.get("operator") or {}).get("open_id", ""),
        "message_id": header.get("event_id") or real_msg_id,
        "_reply_id": real_msg_id,
        "_card_value": value if isinstance(value, dict) else {},
        "_action_token": str(event.get("token") or "")[:200],
    }


def parsed_to_inbound(parsed: dict, raw_event: Optional[dict] = None) -> InboundMessage:
    """_parse_event 产出的 parsed dict → InboundMessage。

    parsed 契约字段（feishu_async.py L875-884）：chat_id / chat_type /
    sender_open_id / message_id / msg_type / text / image_key / file_key。
    群内 @ 剥离后 text 为空 → addressed_to_bot=False（L866 语义）。
    """
    chat_type = _chat_type_of(parsed.get("chat_type", "p2p"))
    text = parsed.get("text", "")
    media_refs: list[MediaRef] = []
    msg_type = _msg_type_of(parsed.get("msg_type", "text"))
    if msg_type == MsgType.IMAGE:
        key = parsed.get("image_key", "")
        if key:
            media_refs.append(MediaRef(type=MsgType.IMAGE, storage_key=key))
    elif msg_type in (MsgType.MEDIA, MsgType.FILE):
        key = parsed.get("file_key", "")
        if key:
            media_refs.append(MediaRef(type=msg_type, storage_key=key))

    # 群消息 @ 剥离后为空 → 未被 @ 裁决（L866：@_user 剥离后空则跳过）
    addressed = True
    if chat_type == ChatType.GROUP:
        addressed = bool(parsed.get("mention_all_stripped", text).strip())

    command_text = parsed.get("command_text", text)  # 命令富化前原文（防二次解释）

    return InboundMessage(
        event_id=parsed.get("event_id", "") or parsed.get("message_id", ""),
        message_id=parsed.get("message_id", ""),
        source=Source(
            channel_type=Type.FEISHU,
            chat_id=parsed.get("chat_id", ""),
            chat_type=chat_type,
            sender_id=parsed.get("sender_open_id", ""),
            sender_stable_id=parsed.get("sender_union_id", ""),
        ),
        type=msg_type,
        text=text,
        command_text=command_text,
        media_refs=media_refs,
        # 卡片按钮回调（card.action.trigger）没有真实 message_id：真实 open_message_id 在
        # _parse_card_action 的 _reply_id 里（event_id 只用于去重），回复必须用它。
        reply_to=ReplyCtx(message_id=str(parsed.get("reply_to_message_id") or parsed.get("_reply_id") or "")
                          ) if (parsed.get("reply_to_message_id") or parsed.get("_reply_id")) else None,
        addressed_to_bot=addressed,
        force_fresh=bool(parsed.get("force_fresh", False)),
        skip_agent_run=bool(parsed.get("skip_agent_run", False)),
        raw=raw_event or dict(parsed),
    )


__all__ = ["parsed_to_inbound", "parse_card_action"]
