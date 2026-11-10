"""Slack 入站翻译 —— envelope / 事件 / 卡片回调 → InboundMessage 信封（Issue #1309）。

真实 envelope 形态（Socket Mode，设计 §3.1 / §4.2）：

    {"envelope_id": "e-1", "type": "events_api", "payload": {...}}   # payload 可能是 JSON 字符串
    {"envelope_id": "e-2", "type": "interactive", "payload": {...block_actions...}}

events_api.payload（Events API 外层）：

    {"type": "event_callback", "event_id": "Ev123",
     "event": {"type": "app_mention|message", "channel": "C1", "user": "U1",
               "text": "<@U0BOT> hi", "ts": "1710000000.000100",
               "thread_ts": "1710000000.000001", "files": [...]}}

interactive.payload（block_actions 内层）：

    {"type": "block_actions", "user": {"id": "U1"},
     "channel": {"id": "C1"}, "message": {"ts": "...", "thread_ts": "..."},
     "actions": [{"action_id": "bind", "value": "{\\"cmd\\": \\"/bind 2\\"}"}],
     "response_url": "https://..."}

防坑（设计 §4.2，前两个渠道踩过）：
  1. payload / actions[].value **可能是字符串** → 先 json.loads 再判类型；
  2. 任何解析异常不得穿透接收循环（本模块只返回 None / 抛 ValueError 由 channel 兜住）；
  3. 幂等键用 `event_id`（编辑会产生同 ts 新事件）；`ts` 只作线程根/回复目标；
  4. 显式忽略 `bot_message` / `message_changed` / `message_deleted`（否则机器人跟自己对话）；
  5. 群消息剥离 `<@Uxxx>` 提及前缀，剥离后为空 → 未 @ 机器人（引擎群过滤丢弃）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ...channel.message import InboundMessage, MediaRef, ReplyCtx, Source
from ...channel.types import ChatType, MsgType, Type
from .cards import card_token, command_from_value, parse_action_value

log = logging.getLogger(__name__)

# 显式忽略的 message 子类型：机器人自回、编辑、删除、已回复
IGNORED_SUBTYPES = frozenset({
    "bot_message",
    "message_changed",
    "message_deleted",
    "message_replied",
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "pinned_item",
})

_MENTION_RE = re.compile(r"<@[A-Za-z0-9]+(?:\|[^>]*)?>")
_IMAGE_MIME_PREFIX = "image/"
_IMAGE_FILETYPES = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "svg"})
_VIDEO_FILETYPES = frozenset({"mp4", "mov", "avi", "mkv", "webm"})

MAX_EVENT_TEXT = 20000


def parse_json_maybe(value: Any) -> Any:
    """Slack 帧字段（envelope.payload / actions[].value）可能是 JSON 字符串。"""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return None
    return value


def _as_dict(value: Any) -> dict:
    """宽容取字典（字符串/None 形态一律回落空字典，不抛异常）。"""
    parsed = parse_json_maybe(value)
    return parsed if isinstance(parsed, dict) else {}


def chat_type_of(event: dict, channel_id: str) -> ChatType:
    """私聊判定：channel_type=im 或 channel id 以 D 开头（Slack DM 约定）。"""
    if str((event or {}).get("channel_type") or "") == "im":
        return ChatType.P2P
    if str(channel_id or "").startswith("D"):
        return ChatType.P2P
    return ChatType.GROUP


def strip_mentions(text: str) -> tuple[str, bool]:
    """剥离 `<@Uxxx>` 提及；返回 (正文, 是否提及过机器人)。"""
    raw = text or ""
    mentioned = bool(_MENTION_RE.search(raw))
    cleaned = _MENTION_RE.sub("", raw)
    # 去掉剥离后残留的多余空白（含不间断空格）
    cleaned = re.sub(r"[ \t\u00a0]{2,}", " ", cleaned).strip()
    return cleaned, mentioned


def files_to_media(files: Any) -> tuple[list[MediaRef], MsgType]:
    """files[] → MediaRef 列表（只存引用，下载在 worker/适配器 api 侧）+ 消息类型。"""
    refs: list[MediaRef] = []
    msg_type = MsgType.TEXT
    for index, item in enumerate(files or []):
        if not isinstance(item, dict):
            continue
        mimetype = str(item.get("mimetype") or "").lower()
        filetype = str(item.get("filetype") or "").lower()
        url = str(item.get("url_private_download") or item.get("url_private") or "")
        if not url:
            continue
        if mimetype.startswith(_IMAGE_MIME_PREFIX) or filetype in _IMAGE_FILETYPES:
            ref_type = MsgType.IMAGE
        elif filetype in _VIDEO_FILETYPES or mimetype.startswith("video/"):
            ref_type = MsgType.MEDIA
        else:
            ref_type = MsgType.FILE
        refs.append(MediaRef(type=ref_type, storage_key=url, inline_index=index))
        if msg_type == MsgType.TEXT:
            msg_type = ref_type
        elif msg_type != ref_type:
            # 混合附件：以第一个为信封主类型（其余仍走 media_refs，不丢）
            pass
    return refs, msg_type


def event_to_inbound(payload: Any, envelope_id: str = "") -> InboundMessage | None:
    """Events API 外层 payload → InboundMessage；不可翻译返回 None。"""
    data = parse_json_maybe(payload)
    if not isinstance(data, dict):
        return None
    if str(data.get("type") or "") != "event_callback":
        return None
    event = data.get("event")
    event = parse_json_maybe(event)
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
    if event_type not in ("app_mention", "message"):
        return None
    if event.get("hidden"):
        return None
    if str(event.get("bot_id") or "") or str(event.get("subtype") or "") in IGNORED_SUBTYPES:
        # 机器人自回 / 编辑 / 删除事件：显式忽略（防自对话、防同 ts 新事件被当新消息）
        log.debug("[slack] ignored event subtype=%r bot=%r",
                  event.get("subtype"), event.get("bot_id"))
        return None

    channel_id = str(event.get("channel") or "")
    user_id = str(event.get("user") or "")
    ts = str(event.get("ts") or "")
    if not channel_id or not ts:
        return None

    thread_ts = str(event.get("thread_ts") or "")
    chat_type = chat_type_of(event, channel_id)
    raw_text = str(event.get("text") or "")[:MAX_EVENT_TEXT]
    text, mentioned = strip_mentions(raw_text)
    media_refs, media_type = files_to_media(event.get("files"))

    if event_type == "app_mention":
        addressed = True
    else:
        addressed = mentioned or chat_type == ChatType.P2P
    if not channel_id.startswith("D") and not text and not media_refs:
        addressed = addressed and bool(mentioned)

    event_id = str(data.get("event_id") or envelope_id or ts)
    msg_type = media_type if media_refs else MsgType.TEXT
    return InboundMessage(
        event_id=event_id,               # 幂等键（Slack 重投同 id；编辑会产生新事件）
        message_id=ts,                   # 平台消息 ID：线程根/回复目标（chat.update 也要它）
        source=Source(
            channel_type=Type.SLACK,
            chat_id=channel_id,
            chat_type=chat_type,
            thread_root=thread_ts,       # 有话题平台：严禁与裸 chat_id 塌缩（§5.2）
            sender_id=user_id,
        ),
        type=msg_type,
        text=text,
        command_text=text,
        media_refs=media_refs,
        reply_to=ReplyCtx(message_id=ts, root_message_id=thread_ts),
        addressed_to_bot=addressed,
        raw=data,
    )


def block_actions_to_inbound(payload: Any, envelope_id: str = "") -> InboundMessage | None:
    """Block Kit 按钮回调 → InboundMessage（按钮点击变成一条"用户命令"）。

    `actions[0].value` 是我们渲染时写入的 `action_payload()` JSON 串；
    回收后 `text` 就是 `"/bind 2"`，与"回数字"走同一份 `_handle_selection`。

    `raw` 额外带两个派生键（装配层 `_raw_card_context` 的既有回退口）：
      - `action_value`：载荷字典（/repo /bind 等命令读它）；
      - `token`：`<channel>|<ts>`，供 `chat.update` 原地更新结果卡。
    """
    data = parse_json_maybe(payload)
    if not isinstance(data, dict):
        return None
    if str(data.get("type") or "") != "block_actions":
        return None
    actions = data.get("actions") or []
    if not isinstance(actions, list) or not actions:
        return None
    action = actions[0] if isinstance(actions[0], dict) else {}
    action_id = str(action.get("action_id") or "")
    value = action.get("value")
    command = command_from_value(value, action_id)
    if not command:
        return None

    channel = _as_dict(data.get("channel"))
    channel_id = str(channel.get("id") or "")
    message = _as_dict(data.get("message"))
    ts = str(message.get("ts") or "")
    if not channel_id or not ts:
        return None
    thread_ts = str(message.get("thread_ts") or "")
    user = _as_dict(data.get("user"))
    user_id = str(user.get("id") or "")

    dedup = f"card:{channel_id}:{ts}:{user_id}:{action_id or command}:{command}"
    action_value = parse_action_value(value)
    raw = dict(data)
    raw["action_value"] = action_value
    raw["token"] = card_token(channel_id, ts)
    event_id = str(data.get("event_id") or envelope_id or dedup)
    return InboundMessage(
        event_id=event_id,
        message_id=dedup,
        source=Source(
            channel_type=Type.SLACK,
            chat_id=channel_id,
            chat_type=chat_type_of({}, channel_id),
            thread_root=thread_ts,
            sender_id=user_id,
        ),
        type=MsgType.CARD_ACTION,
        text=command,
        command_text=command,
        reply_to=ReplyCtx(message_id=ts, root_message_id=thread_ts),
        addressed_to_bot=True,
        raw=raw,
    )


def envelope_payload(envelope: dict) -> tuple[str, Any]:
    """envelope → (type, payload)；payload 可能是字符串（统一解析）。"""
    if not isinstance(envelope, dict):
        return "", None
    return str(envelope.get("type") or ""), parse_json_maybe(envelope.get("payload"))


__all__ = [
    "IGNORED_SUBTYPES",
    "block_actions_to_inbound",
    "chat_type_of",
    "envelope_payload",
    "event_to_inbound",
    "files_to_media",
    "parse_json_maybe",
    "strip_mentions",
]
