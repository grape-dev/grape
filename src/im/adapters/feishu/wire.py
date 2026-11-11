"""飞书渠道共享的解析/去重辅助（原 feishu_async.py 抽出）。

这些函数与"旧 BotManager 传输层"无关：新引擎的 FeishuChannel / 飞书入站解析 /
卡片回调路由 / 扫码 onboard 都还在用。Issue: 下线旧 IM 渠道代码（A2）M1 ——
抽到适配器侧后 feishu_async.py 只剩 API 客户端。内容为原实现逐行搬运，未改行为。
"""
from __future__ import annotations

import json
import logging
import re
import struct
from typing import Any, Optional

from lark_oapi.ws.enum import FrameType, MessageType
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from src.config import log  # noqa: F401  沿用原模块日志出口


def _get_header(headers, key: str) -> str:
    for h in headers:
        if h.key == key:
            return h.value
    return ""


def _build_ack_frame(frame) -> bytes:
    """构造飞书 WS ack 帧：payload 替换为 {"code":200}，其余字段不变，原样回传。

    飞书 WS 长连接要求对每条 DATA(EVENT) 帧回传 ack，否则认为消息未送达，
    会用新的 message_id 反复投递（导致消息重复消费）。ack 帧 = 原帧 payload
    替换为 ack 响应后序列化回传，headers（含 message_id）保持不变。
    """
    frame.payload = json.dumps({"code": 200}).encode("utf-8")
    return frame.SerializeToString()


def _parse_card_action(event_data: dict) -> dict | None:
    """解析飞书消息快捷菜单 / 卡片按钮点击事件（card.action.trigger）

    快捷菜单点击或卡片按钮点击时，飞书推送 card.action.trigger 事件。
    提取 action.value 中的 cmd 字段作为命令文本，等同于用户发送了该命令。
    """
    header = event_data.get("header", {})
    event_type = header.get("event_type", "")
    # CARD 类型事件可能无 header，直接从 event_data 取 action
    if event_type != "card.action.trigger":
        if "action" not in event_data and "action" not in event_data.get("event", {}):
            return None
    event = event_data.get("event", {})
    action = event.get("action", event_data.get("action", {}))
    value_raw = action.get("value", "{}")
    try:
        value = json.loads(value_raw) if isinstance(value_raw, str) else (value_raw or {})
    except (json.JSONDecodeError, TypeError):
        value = {}
    # select_static 组件：选中值优先从 action.option 取（新选中值），
    # action.value 是 select 组件的当前 value 字段（初始值或上一次选中值）——
    # 第一次选中时 value 仍是旧值，option 才是真正新选的。
    _action_tag = action.get("tag", "")
    if _action_tag == "select_static" or "option" in action or "selected_option" in action:
        _opt_raw = action.get("option")
        if isinstance(_opt_raw, str):
            try:
                _opt_raw = json.loads(_opt_raw)
            except (json.JSONDecodeError, TypeError):
                _opt_raw = None
        if isinstance(_opt_raw, dict) and _opt_raw.get("cmd"):
            value = _opt_raw
        elif isinstance(action.get("selected_option"), dict):
            _sel = action["selected_option"].get("value", {})
            if isinstance(_sel, str):
                try:
                    _sel = json.loads(_sel)
                except (json.JSONDecodeError, TypeError):
                    _sel = None
            if isinstance(_sel, dict) and _sel.get("cmd"):
                value = _sel
    # 卡片 2.0 按钮：回调值在 behaviors[].value（type=callback；不在 action.value）。
    # 兼容 behaviors[].callback.value 包装形态（部分事件网关会包一层）。
    if not (value or {}).get("cmd"):
        for _bh in (action.get("behaviors") or []):
            if not isinstance(_bh, dict):
                continue
            _bv = _bh.get("value")
            if not (isinstance(_bv, dict) and _bv.get("cmd")):
                _bv = (_bh.get("callback") or {}).get("value", {})
            if isinstance(_bv, dict) and _bv.get("cmd"):
                value = {**value, **_bv} if isinstance(value, dict) else dict(_bv)
                break
    cmd = (value.get("cmd") or "").strip()
    if not cmd:
        log.info(f"card.action.trigger without cmd: {value_raw}")
        return None
    # card.action.trigger 字段结构：
    #   operator.open_id = 点击用户
    #   context.open_chat_id / open_message_id
    operator = event.get("operator", {}) or {}
    context_ = event.get("context", {}) or {}
    sender_open_id = operator.get("open_id", "") or event.get("open_id") or event_data.get("open_id") or ""
    chat_id = context_.get("open_chat_id") or event.get("open_chat_id", event.get("chat_id", "")) or ""
    msg_id = context_.get("open_message_id") or event.get("open_message_id") or ""
    return {
        "type": "message",
        "text": cmd,
        "chat_id": chat_id,
        "chat_type": "p2p",
        "sender_id": event.get("user_id") or event_data.get("user_id") or "",
        "sender_open_id": sender_open_id,
        "message_id": header.get("event_id") or msg_id,  # event_id 唯一，用于去重
        "_reply_id": msg_id,  # 真实消息 ID，用于回复
        "message_type": "text",
        "_card_value": value if isinstance(value, dict) else {},  # 按钮附加字段（cmd 之外的 目标上下文）
        # 延迟更新令牌（interactive/v1/card/update 用，30 分钟有效、每次交互刷新）：
        # 供"卡片原地状态流转"方案（选问题→选场景→确认 同一张卡）使用
        "_action_token": str(event.get("token") or "")[:200],
    }


def _parse_event(event_data: dict) -> dict | None:
    """从飞书事件 JSON 提取标准化消息 dict"""
    header = event_data.get("header", {})
    event_type = header.get("event_type", "")

    if event_type != "im.message.receive_v1":
        # 尝试解析 card.action.trigger（可能无 header，直接从 data 取）
        if event_type == "card.action.trigger" or header.get("event_type") is None:
            return _parse_card_action(event_data)
        return None

    event = event_data.get("event", {})
    message = event.get("message", {})
    sender = event.get("sender", {})
    msg_content = message.get("content", "{}") or "{}"
    msg_type = message.get("message_type", "text") or "text"

    text = ""
    image_key = ""
    file_key = ""
    if msg_type == "text":
        try:
            text = json.loads(msg_content).get("text", "")
        except json.JSONDecodeError:
            text = msg_content
    elif msg_type == "post":
        try:
            content_data = json.loads(msg_content)
            content_body = content_data.get("content", {})
            if isinstance(content_body, dict):
                for lang_data in content_body.values():
                    if isinstance(lang_data, list):
                        for paragraph in lang_data:
                            if isinstance(paragraph, list):
                                for segment in paragraph:
                                    if segment.get("tag") == "text":
                                        text += segment.get("text", "") + " "
            elif isinstance(content_body, list):
                for paragraph in content_body:
                    if isinstance(paragraph, list):
                        for segment in paragraph:
                            if segment.get("tag") == "text":
                                text += segment.get("text", "") + " "
        except (json.JSONDecodeError, KeyError, TypeError):
            text = msg_content

    elif msg_type == "image":
        # 图片消息：提取 image_key，后续由 handler 下载并理解
        try:
            img_data = json.loads(msg_content)
            image_key = img_data.get("image_key", "")
            if image_key:
                text = "[图片]"
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    elif msg_type == "media":
        # 视频/文件消息：提取 file_key，后续由 handler 下载并理解
        try:
            media_data = json.loads(msg_content)
            file_key = media_data.get("file_key", "")
            if file_key:
                text = "[视频]"
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    elif msg_type == "file":
        # 文件消息：提取 file_key
        try:
            file_data = json.loads(msg_content)
            file_key = file_data.get("file_key", "")
            if file_key:
                text = "[文件]"
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    text = re.sub(r'@_?\w+\s*', '', text).strip()

    sender_id = ""
    sender_open_id = ""
    sender_id_obj = sender.get("sender_id", {})
    if sender_id_obj:
        sender_id = sender_id_obj.get("user_id", "") or ""
        sender_open_id = sender_id_obj.get("open_id", "") or ""

    return {
        "type": "message",
        "text": text,
        "chat_id": message.get("chat_id", ""),
        "chat_type": message.get("chat_type", ""),  # p2p=私聊 / group=群聊
        "sender_id": sender_id,
        "sender_open_id": sender_open_id,
        "message_id": message.get("message_id", ""),
        "message_type": msg_type,
        "image_key": image_key if msg_type == "image" else "",
        "file_key": file_key if msg_type in ("media", "file") else "",
    }
