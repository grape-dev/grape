"""企微入站翻译 —— 长连接 JSON 帧 → InboundMessage 信封（Issue #1254）。

协议事实（官方文档 /document/path/101463，2026-10 版）：
  - 信封：{cmd, headers:{req_id}, body}；**body 可能是 JSON 字符串**（钉钉 #1008 教训）；
  - 消息回调 body：msgid / aibotid / chatid（仅群聊）/ chattype("single"|"group") /
    from.userid / msgtype(text|image|mixed|voice|file|video) / 对应类型子结构；
  - 事件回调 body：msgtype="event" + event.eventtype（enter_chat / template_card_event /
    feedback_event / disconnected_event）；
  - 媒体：image/file/video 子结构带 {url, aeskey}，url **5 分钟有效**、aeskey 每条唯一
    （AES-256-CBC / PKCS#7 补至 32 字节 / IV=aeskey 前 16 字节）；
  - 语音：企微侧已转文字，voice.content 直接是转写文本（不下载、不解密）。

BindingKey（§5.2 企微行）= chatid（群聊）/ from.userid（单聊，企微单聊无 chatid）。
附件两跳（P4）：本模块只产出 MediaItem 引用，下载在 channel 收帧时同步完成
（URL 5 分钟过期，不能延后），理解仍在 worker 侧。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from ...channel.message import InboundMessage, MediaRef, ReplyCtx, Source
from ...channel.types import ChatType, MsgType, Type

log = logging.getLogger(__name__)

WECOM_WS_URL = "wss://openws.work.weixin.qq.com"

CMD_SUBSCRIBE = "aibot_subscribe"
CMD_MSG_CALLBACK = "aibot_msg_callback"
CMD_EVENT_CALLBACK = "aibot_event_callback"
CMD_RESPOND_MSG = "aibot_respond_msg"
CMD_SEND_MSG = "aibot_send_msg"
CMD_RESPOND_UPDATE_MSG = "aibot_respond_update_msg"
CMD_RESPOND_WELCOME_MSG = "aibot_respond_welcome_msg"
CMD_UPLOAD_MEDIA_INIT = "aibot_upload_media_init"
CMD_UPLOAD_MEDIA_CHUNK = "aibot_upload_media_chunk"
CMD_UPLOAD_MEDIA_FINISH = "aibot_upload_media_finish"
CMD_PING = "ping"

EVENT_TEMPLATE_CARD = "template_card_event"
EVENT_DISCONNECTED = "disconnected_event"

_MSG_TYPE_MAP = {
    "text": MsgType.TEXT,
    "voice": MsgType.TEXT,      # 企微转写文本 → 纯文本（不下载音频）
    "image": MsgType.IMAGE,
    "mixed": MsgType.TEXT,
    "file": MsgType.FILE,
    "video": MsgType.MEDIA,
}


@dataclass(frozen=True)
class MediaItem:
    """入站媒体引用（url + 独立 aeskey，5 分钟有效 → 必须收帧即下载）。"""

    type: MsgType
    url: str
    aeskey: str = ""
    filename: str = ""


# ---------------------------------------------------------------- 帧解析

def parse_frame(raw: Any) -> dict:
    """任意输入 → dict 帧（非法帧返回 {}，绝不抛异常给接收循环）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            return {}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _as_dict(value: Any) -> dict:
    """body / 子结构容错：dict 直接用；JSON 字符串先 loads（字符串信封是真实形态）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return parse_frame(value)
    return {}


def frame_body(payload: dict) -> dict:
    return _as_dict((payload or {}).get("body"))


def frame_req_id(payload: dict) -> str:
    headers = (payload or {}).get("headers")
    if not isinstance(headers, dict):
        return ""
    return str(headers.get("req_id") or "")


def frame_cmd(payload: dict) -> str:
    return str((payload or {}).get("cmd") or "")


def is_ack_frame(payload: dict) -> bool:
    """ack（订阅/发送/上传的响应）：无 cmd、带 errcode、headers.req_id 透传。"""
    return (not frame_cmd(payload)) and ("errcode" in (payload or {}))


# ---------------------------------------------------------------- 媒体

def media_items(payload: dict) -> list[MediaItem]:
    """帧 → 待下载媒体列表（图片/文件/视频；图文混排逐项；语音不是媒体）。

    顺序即 inline_index 顺序：失败时不得移位（multica 陷阱 7）。
    """
    body = frame_body(payload)
    msgtype = str(body.get("msgtype") or "")
    items: list[MediaItem] = []

    def _add(kind: MsgType, node: Any) -> None:
        node = _as_dict(node)
        url = str(node.get("url") or "")
        if not url:
            return
        items.append(MediaItem(
            type=kind, url=url, aeskey=str(node.get("aeskey") or ""),
            filename=str(node.get("filename") or node.get("file_name") or "")))

    if msgtype == "image":
        _add(MsgType.IMAGE, body.get("image"))
    elif msgtype == "file":
        _add(MsgType.FILE, body.get("file"))
    elif msgtype == "video":
        _add(MsgType.MEDIA, body.get("video"))
    elif msgtype == "mixed":
        mixed = _as_dict(body.get("mixed"))
        for item in mixed.get("msg_item") or []:
            item = _as_dict(item)
            kind = str(item.get("msgtype") or "")
            if kind == "image":
                _add(MsgType.IMAGE, item.get("image"))
    return items


# ---------------------------------------------------------------- 文本

def _chat_type_of(value: Any) -> ChatType:
    # 企微 chattype 是字符串 "single" / "group"（不是 1/2）
    return ChatType.GROUP if str(value or "").strip().lower() == "group" else ChatType.P2P


def _extract_text(body: dict) -> str:
    msgtype = str(body.get("msgtype") or "")
    if msgtype == "text":
        node = _as_dict(body.get("text"))
        return str(node.get("content") or "").strip()
    if msgtype == "voice":
        node = _as_dict(body.get("voice"))
        return str(node.get("content") or node.get("text") or "").strip()
    if msgtype == "mixed":
        parts: list[str] = []
        mixed = _as_dict(body.get("mixed"))
        for item in mixed.get("msg_item") or []:
            item = _as_dict(item)
            if str(item.get("msgtype") or "") == "text":
                node = _as_dict(item.get("text"))
                content = str(node.get("content") or "").strip()
                if content:
                    parts.append(content)
        return "\n".join(parts).strip()
    return ""


_MENTION_RE = re.compile(r"^@(?P<name>\S+?)(?:\s+(?P<rest>.*))?$", re.S)


def strip_mention(text: str, bot_name: str = "") -> tuple[str, bool]:
    """群内 @ 前缀剥离 → (正文, 是否 @ 了机器人)。

    官方群聊正文形如 "@RobotA hello"（@ 后面紧跟机器人显示名，无独立 mention 字段）；
    `bot_name` 可用时必须匹配该名字，否则「@其他人」会被误当成 @ 机器人派发。
    `bot_name` 为空时无法校验身份（企微未下发机器人名），保持旧口径「首 token 是 @ 即
    视为 @ 机器人」——官方仅在群内 @ 机器人时回调，兼容无名字配置的存量部署。
    分隔符按 `\\s` 处理（全角空格/不间断空格同样适用），剥离后为空 → 视为「只 @ 没说话」。
    """
    if not text.startswith("@"):
        return text, False
    match = _MENTION_RE.match(text)
    if match is None:
        return text, False
    name = match.group("name").strip()
    rest = (match.group("rest") or "").strip()
    expected = str(bot_name or "").strip().lstrip("@")
    if expected and name.casefold() != expected.casefold():
        # 不是 @ 本机器人：保留原文交由上层忽略（addressed=False），不当成正文吞掉
        return text, False
    if not rest:
        return "", True
    return rest, True


def _sender_id(body: dict) -> str:
    frm = _as_dict(body.get("from"))
    return str(frm.get("userid") or frm.get("user_id") or "")


def _chat_id(body: dict, chat_type: ChatType, sender: str) -> str:
    chatid = str(body.get("chatid") or "")
    if chatid:
        return chatid
    # 单聊企微不下发 chatid → 用 sender userid 作为路由主体（§5.2）
    return sender if chat_type == ChatType.P2P else ""


def _media_refs(items: list[MediaItem], media_keys: list[str] | None) -> list[MediaRef]:
    refs: list[MediaRef] = []
    keys = list(media_keys or [])
    for idx, item in enumerate(items):
        key = keys[idx] if idx < len(keys) else ""
        if not key:
            # 下载失败：记日志、跳过该附件，**不移位**后续索引
            log.warning("[wecom] media download missing (type=%s url=%s)",
                        item.type.value, item.url[:60])
            continue
        refs.append(MediaRef(type=item.type, storage_key=key, inline_index=idx))
    return refs


# ---------------------------------------------------------------- 消息帧

def event_to_inbound(payload: dict, *, media_keys: list[str] | None = None,
                     bot_name: str = "") -> InboundMessage | None:
    """aibot_msg_callback → InboundMessage；其他帧（事件/ack）返回 None。

    解析绝不抛异常：任何结构异常 → 记日志返回 None（channel 层计数
    `parse_fail`，接收循环不能因单帧坏数据猝死）。
    """
    try:
        if frame_cmd(payload) != CMD_MSG_CALLBACK:
            return None
        body = frame_body(payload)
        if not body:
            log.warning("[wecom] msg callback without parsable body")
            return None
        msg_id = str(body.get("msgid") or "")
        if not msg_id:
            log.warning("[wecom] msg callback without msgid, dropped frame")
            return None

        chat_type = _chat_type_of(body.get("chattype"))
        sender = _sender_id(body)
        chat_id = _chat_id(body, chat_type, sender)
        if not chat_id:
            log.warning("[wecom] msg callback without route (msgid=%s)", msg_id)
            return None

        raw_text = _extract_text(body)
        text = raw_text
        addressed = True
        if chat_type == ChatType.GROUP:
            text, mentioned = strip_mention(raw_text, bot_name)
            items = media_items(payload)
            # 群聊只认 @：@ 了（哪怕只 @ 不说话）或纯媒体消息（企微 @ 图片不带文本）才派发
            addressed = mentioned or (not raw_text and bool(items))

        msg_type = _MSG_TYPE_MAP.get(str(body.get("msgtype") or ""), MsgType.UNKNOWN)
        if msg_type is MsgType.UNKNOWN:
            # 未知 msgtype 不静默：保留日志 + 回落到 TEXT（下游按 text 处理并回执）
            log.info("[wecom] unknown msgtype %r (msgid=%s) → TEXT fallback",
                     body.get("msgtype"), msg_id)
            msg_type = MsgType.TEXT

        req_id = frame_req_id(payload)
        items = media_items(payload)
        refs = _media_refs(items, media_keys)
        if items and not refs:
            # 有媒体但一个都没落地：不静默 —— 明确告知解析层缺失
            log.warning("[wecom] all media downloads failed (msgid=%s items=%d)",
                        msg_id, len(items))

        return InboundMessage(
            event_id=msg_id,
            message_id=msg_id,
            source=Source(
                channel_type=Type.WECOM,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_id=sender,
            ),
            type=msg_type,
            text=text,
            command_text=text,
            media_refs=refs,
            reply_to=ReplyCtx(message_id=req_id, root_message_id=msg_id) if req_id else None,
            addressed_to_bot=addressed,
            raw=dict(payload),
        )
    except Exception as e:  # 解析异常绝不穿透接收循环
        log.warning("[wecom] inbound parse failed: %r", e)
        return None


# ---------------------------------------------------------------- 卡片事件

def template_card_event_to_inbound(payload: dict,
                                   media_keys: list[str] | None = None) -> InboundMessage | None:
    """template_card_event → InboundMessage(CARD_ACTION)。

    event_key 是渲染期写入的 `action_payload` 序列化（{"cmd": "/bind 2"}），
    这里解回成 `/xxx n` 命令串，走**同一份** `_handle_selection` 路径；
    同时把结构化载荷放进 raw["action_value"]（wiring 的 _raw_card_context 直接读）。
    """
    try:
        if frame_cmd(payload) != CMD_EVENT_CALLBACK:
            return None
        body = frame_body(payload)
        event = _as_dict(body.get("event"))
        if str(event.get("eventtype") or "") != EVENT_TEMPLATE_CARD:
            return None

        card_event = _as_dict(event.get("template_card_event"))
        key = str(card_event.get("event_key") or card_event.get("key") or "")

        from .cards import decode_action_key, event_key_to_command
        value = decode_action_key(key)
        command = event_key_to_command(key)
        if not command:
            log.warning("[wecom] template_card_event without usable event_key (%r)", key[:60])
            return None

        msg_id = str(body.get("msgid") or "") or f"card:{key}"
        chat_type = _chat_type_of(body.get("chattype"))
        sender = _sender_id(body)
        chat_id = _chat_id(body, chat_type, sender)
        if not chat_id:
            # 缺 chat 信息不猜测（无路由无法回卡）
            log.warning("[wecom] card event without route (msgid=%s)", msg_id)
            return None

        req_id = frame_req_id(payload)
        raw = dict(payload)
        if value is not None:
            raw["action_value"] = value
        return InboundMessage(
            event_id=msg_id,
            message_id=msg_id,
            source=Source(
                channel_type=Type.WECOM,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_id=sender,
            ),
            type=MsgType.CARD_ACTION,
            text=command,
            command_text=command,
            media_refs=[],
            reply_to=ReplyCtx(message_id=req_id, root_message_id=msg_id) if req_id else None,
            addressed_to_bot=True,
            raw=raw,
        )
    except Exception as e:
        log.warning("[wecom] card event parse failed: %r", e)
        return None


# ---------------------------------------------------------------- 出站帧

def build_subscribe_frame(bot_id: str, secret: str, req_id: str) -> dict:
    return {"cmd": CMD_SUBSCRIBE, "headers": {"req_id": req_id},
            "body": {"bot_id": str(bot_id or ""), "secret": str(secret or "")}}


def build_ping_frame(req_id: str) -> dict:
    return {"cmd": CMD_PING, "headers": {"req_id": req_id}}


def is_disconnect_event(payload: dict) -> bool:
    """disconnected_event：同一机器人新连接订阅成功 → 服务端踢掉旧连接。"""
    if frame_cmd(payload) != CMD_EVENT_CALLBACK:
        return False
    body = frame_body(payload)
    event = _as_dict(body.get("event"))
    return str(event.get("eventtype") or "") == EVENT_DISCONNECTED


def is_event_frame(payload: dict) -> bool:
    return frame_cmd(payload) == CMD_EVENT_CALLBACK


__all__ = [
    "CMD_EVENT_CALLBACK",
    "CMD_MSG_CALLBACK",
    "CMD_PING",
    "CMD_RESPOND_MSG",
    "CMD_RESPOND_UPDATE_MSG",
    "CMD_RESPOND_WELCOME_MSG",
    "CMD_SEND_MSG",
    "CMD_SUBSCRIBE",
    "CMD_UPLOAD_MEDIA_CHUNK",
    "CMD_UPLOAD_MEDIA_FINISH",
    "CMD_UPLOAD_MEDIA_INIT",
    "EVENT_DISCONNECTED",
    "EVENT_TEMPLATE_CARD",
    "WECOM_WS_URL",
    "MediaItem",
    "build_ping_frame",
    "build_subscribe_frame",
    "event_to_inbound",
    "frame_body",
    "frame_cmd",
    "frame_req_id",
    "is_ack_frame",
    "is_disconnect_event",
    "is_event_frame",
    "media_items",
    "parse_frame",
    "strip_mention",
    "template_card_event_to_inbound",
]