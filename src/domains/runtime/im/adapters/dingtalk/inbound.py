"""钉钉入站翻译 —— DingTalk Stream 事件 payload → InboundMessage 信封（Issue #936 P2）。

golden 契约（Stream 模式 WS 推送的 JSON 帧）：
  headers: messageId / eventId / eventType / contentType / appKey / timestamp
  data:    conversationId / msgId / conversationType ("1"=单聊 "2"=群) /
           senderStaffId / senderNick / msgtype (text|picture|file) /
           text.content / content / downloadCode / sessionId

BindingKey（设计文档 §5.2 钉钉行）= conversation_id（Source.chat_id）。
附件两跳（P4）：downloadCode 只翻译成 MediaRef 引用，下载与理解在 worker 侧。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ...channel.message import InboundMessage, MediaRef, ReplyCtx, Source
from ...channel.types import ChatType, MsgType, Type

log = logging.getLogger(__name__)

_MSG_TYPE_MAP = {
    "text": MsgType.TEXT,
    "picture": MsgType.IMAGE,
    "image": MsgType.IMAGE,
    "photo": MsgType.IMAGE,
    "file": MsgType.FILE,
    "video": MsgType.MEDIA,
    "richText": MsgType.TEXT,
}

# 群内 @ 机器人前缀（钉钉文本正文常带 "@机器人名 " 前缀）


def _chat_type_of(v: Any) -> ChatType:
    # 钉钉 conversationType："1"=单聊（1:1），"2"=群
    return ChatType.GROUP if str(v) == "2" else ChatType.P2P


def _msg_type_of(v: Any) -> MsgType:
    return _MSG_TYPE_MAP.get(str(v or ""), MsgType.UNKNOWN)


def _extract_text(data: dict) -> str:
    """文本正文：text.content 优先，回落 content（富文本/兜底）。"""
    text = data.get("text") or {}
    if isinstance(text, dict):
        content = str(text.get("content", ""))
    else:
        content = str(text or "")
    if not content:
        content = str(data.get("content", "") or "")
    return content.strip()


def _strip_mention(text: str) -> str:
    """群内 @ 前缀剥离（对齐飞书 L866 @剥离语义）：剥离后为空 → 群内未 @ 裁决。"""
    if not text.startswith("@"):
        return text
    # "@机器人名 消息" / "@机器人名消息"：去掉首 token
    parts = text.split(" ", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return ""


def _sender_id(data: dict) -> str:
    """平台用户 ID：senderStaffId（企业内部员工 ID）优先，回落 senderNick。"""
    sid = str(data.get("senderStaffId", "") or "")
    if sid:
        return sid
    return str(data.get("senderNick", "") or "")


def event_to_inbound(payload: dict) -> InboundMessage | None:
    """Stream 事件 JSON → InboundMessage；非 chat 消息（SYSTEM 心跳等）返回 None。"""
    headers = payload.get("headers") if isinstance(payload, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    # 钉钉 Stream 协议里 data 是「JSON 字符串」（官方协议文档 §通用字段说明：
    # data String 推送数据的 json 字符串）。早期实现按 dict 判定，字符串帧会被
    # 静默丢弃（ACK 已回，平台不再重投 → 消息黑洞）。这里两种形态都接受。
    if isinstance(data, str):
        try:
            data = json.loads(data) if data.strip() else {}
        except Exception:
            data = None
    if not isinstance(headers, dict) or not isinstance(data, dict):
        return None
    event_type = str(headers.get("eventType", "") or "")
    topic = str(headers.get("topic", "") or "")
    if event_type and event_type not in ("chat.message",):
        # 钉钉真实帧的 eventType 口径不统一（部分企业应用下发空值或 EVENT），
        # 机器人回调 topic 命中即放行；SYSTEM 心跳/其他事件仍丢弃
        if topic != "/v1.0/im/bot/messages/get":
            return None

    conversation_id = str(data.get("conversationId", "") or "")
    msg_id = str(data.get("msgId", "") or headers.get("messageId", "") or "")
    if not conversation_id or not msg_id:
        return None

    chat_type = _chat_type_of(data.get("conversationType"))
    raw_text = _extract_text(data)
    text = _strip_mention(raw_text) if chat_type == ChatType.GROUP else raw_text
    addressed = True
    if chat_type == ChatType.GROUP:
        addressed = bool(text)

    msg_type = _msg_type_of(data.get("msgtype"))
    if msg_type is MsgType.UNKNOWN:
        # 未知 msgtype 不静默：记 debug 日志（仍回落 TEXT 保持下游兼容）
        log.debug("[dingtalk] unknown msgtype %r (msgId=%s), falling back to TEXT",
                  data.get("msgtype"), msg_id)
    media_refs: list[MediaRef] = []
    if msg_type in (MsgType.IMAGE, MsgType.FILE, MsgType.MEDIA):
        code = str(data.get("downloadCode", "") or data.get("fileCode", "") or "")
        if code:
            media_refs.append(MediaRef(type=msg_type, storage_key=code))

    session_id = str(data.get("sessionId", "") or "")
    reply_to = ReplyCtx(message_id=session_id) if session_id else None

    return InboundMessage(
        event_id=str(headers.get("eventId", "") or msg_id),
        message_id=msg_id,
        source=Source(
            channel_type=Type.DINGTALK,
            chat_id=conversation_id,          # BindingKey = conversation_id（§5.2）
            chat_type=chat_type,
            sender_id=_sender_id(data),
        ),
        type=msg_type if msg_type != MsgType.UNKNOWN else MsgType.TEXT,
        text=text,
        command_text=text,
        media_refs=media_refs,
        reply_to=reply_to,
        addressed_to_bot=addressed,
        raw=payload,
    )


CARD_CALLBACK_TOPIC = "/v1.0/card/instances/callback"


def _json_maybe(v: Any) -> Any:
    """钉钉 Stream 帧的 data / content 都是「JSON 字符串」（同 data 坑）。"""
    if isinstance(v, str):
        try:
            return json.loads(v) if v.strip() else {}
        except Exception:
            return None
    return v


def card_callback_to_inbound(payload: dict, chat_resolver=None) -> InboundMessage | None:
    """互动卡片按钮回调帧 → InboundMessage（批 5：按钮点击变成一条"用户命令"）。

    帧形态（官方 · 事件回调 / STREAM）：
      headers.topic = /v1.0/card/instances/callback
      data = {
        "type": "actionCallback", "outTrackId": ..., "userId": ..., "corpId": ...,
        "content": "{...}"   # JSON 字符串
      }
      content = {type, outTrackId, userId, cardPrivateData:{actionIds:[...], params:{...}}}

    回调里**不带 conversationId**，所以 chat_id 由我们渲染模板时写进按钮 params
    （`params.chat`），不依赖未文档化字段。params 约定（渲染侧负责填）：
      {"action":"select", "cmd":"bind|join", "num":1..N, "chat":"<convId>"}
      {"action":"cancel", "chat": ...}
      {"action":"page",  "cmd":"repo|chat|task", "page":2, "chat": ...}
    """
    headers = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(headers, dict):
        return None
    topic = str(headers.get("topic", "") or "")
    if topic != CARD_CALLBACK_TOPIC:
        return None
    data = _json_maybe(payload.get("data"))
    if not isinstance(data, dict):
        return None
    if str(data.get("type", "")) != "actionCallback":
        return None
    content = _json_maybe(data.get("content"))
    if not isinstance(content, dict):
        return None
    private = content.get("cardPrivateData") or {}
    if not isinstance(private, dict):
        return None
    params = private.get("params") or {}
    if isinstance(params, str):
        params = _json_maybe(params) or {}
    if not isinstance(params, dict):
        params = {}

    chat_id = str(params.get("chat", "") or "")
    out_track_id = str(content.get("outTrackId", "") or data.get("outTrackId", "") or "")
    if not chat_id and chat_resolver is not None and out_track_id:
        # 模板按钮的 params 是固定值，无法带会话：用 outTrackId → 会话 的落库映射反查
        # （批 5.2 在投放时写 grape_t_channel_outbound_card；这里只依赖注入的 resolver）
        try:
            chat_id = str(chat_resolver(out_track_id) or "")
        except Exception:
            chat_id = ""
    if not chat_id:
        # 无 chat 参数且查不到映射 = 不是我们投放的卡，忽略而不是猜
        return None
    user_id = str(content.get("userId", "") or data.get("userId", "") or "")
    action = str(params.get("action", "") or "")
    num = params.get("num")
    cmd_name = str(params.get("cmd", "") or "")

    if action == "cancel":
        text = "/cancel"
    elif action == "select" and num is not None:
        # 模板按钮只带 num（固定 params）→ 发纯数字，走引擎既有的 pending 选择分支
        # （repo/chat/task 由 pending.action 决定，不必在模板里编码业务类型）
        text = f"/{cmd_name} {num}" if cmd_name else str(num)
    elif action == "page" and cmd_name:
        text = f"/{cmd_name} {params.get('page', 1)}"     # 批 5.2：命令侧识别页码参数
    else:
        log.info("[dingtalk] card callback with unknown params: %r", params)
        return None

    action_ids = private.get("actionIds") or []
    dedup_key = f"card:{out_track_id}:{','.join(map(str, action_ids))}:{text}"
    return InboundMessage(
        event_id=dedup_key,
        message_id=dedup_key,
        source=Source(
            channel_type=Type.DINGTALK,
            chat_id=chat_id,
            chat_type=ChatType.P2P,      # 卡片回调不区分群/单聊；群过滤不该拦命令
            sender_id=user_id,
        ),
        type=MsgType.TEXT,
        text=text,
        command_text=text,
        addressed_to_bot=True,
        raw=payload,
    )


def out_track_id_of(payload: dict) -> str:
    """从卡片回调帧取 outTrackId（data 可能是 JSON 字符串）。"""
    data = _json_maybe((payload or {}).get("data")) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("outTrackId", "") or "")


def build_ack_frame(payload: dict, message: str = "OK") -> dict:
    """钉钉 Stream ACK 帧（3s 时限）：对齐官方 SDK AckMessage.to_dict()。

    - data 必须是 JSON 字符串（不是对象）；
    - 需要 message 字段；
    缺这两项会被判为无效 ACK，同一消息按 60s 周期反复重投（2026-11-04 实测）。
    """
    headers = payload.get("headers") or {}
    message_id = str(headers.get("messageId", "") or "")
    return {
        "code": 200,
        "headers": {"contentType": "application/json", "messageId": message_id},
        "message": message,
        "data": json.dumps({"response": message}),
    }


__all__ = ["build_ack_frame", "event_to_inbound", "card_callback_to_inbound",
           "out_track_id_of", "CARD_CALLBACK_TOPIC"]
