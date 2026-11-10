"""企微入站翻译测试（Issue #1254）。

fixture 用**真实报文形态**（钉钉两次事故的教训）：
  - `body` 既覆盖 dict 也覆盖 **JSON 字符串**（长连接推送实际是字符串信封）；
  - 带 `chattype`（single/group）与 `chatid`（仅群聊）；
  - 消息帧**没有** eventtype 字段；事件帧才是 `msgtype="event"` + `event.eventtype`；
  - 媒体子结构带 `url` + 独立 `aeskey`。
"""
import json

import pytest

from src.im.adapters.wecom.inbound import (
    event_to_inbound,
    media_items,
    parse_frame,
    strip_mention,
    template_card_event_to_inbound,
)
from src.im.channel.types import ChatType, MsgType, Type


def _msg_frame(msgtype="text", chattype="single", text="@RobotA hello robot",
               chatid="", msgid="mid_1", body_extra=None, as_string=True):
    body = {
        "msgid": msgid,
        "aibotid": "bot_abc",
        "chattype": chattype,
        "from": {"userid": "zhangsan"},
        "msgtype": msgtype,
    }
    if chatid:
        body["chatid"] = chatid
    if msgtype == "text":
        body["text"] = {"content": text}
    body.update(body_extra or {})
    payload = {"cmd": "aibot_msg_callback", "headers": {"req_id": "req-1"}}
    payload["body"] = json.dumps(body, ensure_ascii=False) if as_string else body
    return payload


def _card_event(key='{"cmd": "/bind 2"}', chattype="group", chatid="chat_9"):
    body = {
        "msgid": "evt_1",
        "create_time": 1700000000,
        "aibotid": "bot_abc",
        "chattype": chattype,
        "from": {"userid": "lisi"},
        "msgtype": "event",
        "event": {"eventtype": "template_card_event",
                  "template_card_event": {"card_type": "button_interaction",
                                          "task_id": "task_x", "event_key": key}},
    }
    if chatid:
        body["chatid"] = chatid
    return {"cmd": "aibot_event_callback",
            "headers": {"req_id": "req-card"}, "body": json.dumps(body)}


# ---------------------------------------------------------------- 文本

def test_parse_frame_accepts_string_and_dict():
    assert parse_frame('{"cmd": "ping"}') == {"cmd": "ping"}
    assert parse_frame({"cmd": "ping"}) == {"cmd": "ping"}
    assert parse_frame(b'{"cmd": "ping"}') == {"cmd": "ping"}
    assert parse_frame("not json") == {}
    assert parse_frame(None) == {}


def test_single_chat_text_routes_by_sender_userid():
    msg = event_to_inbound(_msg_frame(text="你好", chattype="single"))
    assert msg is not None
    assert msg.source.channel_type is Type.WECOM
    assert msg.source.chat_type is ChatType.P2P
    assert msg.source.chat_id == "zhangsan"      # 单聊无 chatid → sender 作路由
    assert msg.source.sender_id == "zhangsan"
    assert msg.text == "你好"                    # 单聊不剥 @
    assert msg.type is MsgType.TEXT
    assert msg.addressed_to_bot is True
    assert msg.message_id == "mid_1"
    assert msg.reply_to and msg.reply_to.message_id == "req-1"


def test_group_chat_text_at_bot_is_stripped_and_addressed():
    msg = event_to_inbound(_msg_frame(text="@RobotA hello robot", chattype="group",
                                      chatid="chat_1"))
    assert msg is not None
    assert msg.source.chat_type is ChatType.GROUP
    assert msg.source.chat_id == "chat_1"        # 群聊 BindingKey = chatid
    assert msg.text == "hello robot"
    assert msg.addressed_to_bot is True


def test_group_chat_without_at_is_not_addressed():
    msg = event_to_inbound(_msg_frame(text="hello robot", chattype="group",
                                      chatid="chat_1"))
    assert msg is not None
    assert msg.addressed_to_bot is False


def test_group_chat_at_only_is_addressed_but_empty():
    msg = event_to_inbound(_msg_frame(text="@RobotA", chattype="group", chatid="chat_1"))
    assert msg is not None
    assert msg.text == ""
    assert msg.addressed_to_bot is True


def test_strip_mention_helper():
    assert strip_mention("@bot hi") == ("hi", True)
    assert strip_mention("@bot") == ("", True)
    assert strip_mention("hi") == ("hi", False)


def test_group_at_other_user_is_not_addressed_when_bot_name_known():
    """群内 @ 其他人：已知机器人名时必须判 addressed=False，不能误派发。"""
    msg = event_to_inbound(_msg_frame(text="@Alice hello", chattype="group",
                                      chatid="chat_1"), bot_name="RobotA")
    assert msg is not None
    assert msg.addressed_to_bot is False
    assert msg.text == "@Alice hello"          # 原文保留，不被当成正文吞掉


def test_group_at_bot_name_is_addressed_and_stripped():
    msg = event_to_inbound(_msg_frame(text="@RobotA hello", chattype="group",
                                      chatid="chat_1"), bot_name="RobotA")
    assert msg is not None
    assert msg.addressed_to_bot is True
    assert msg.text == "hello"


def test_strip_mention_handles_unicode_separator():
    assert strip_mention("@RobotA　hello", "RobotA") == ("hello", True)
    assert strip_mention("@RobotA\u00a0hello", "RobotA") == ("hello", True)


def test_body_as_dict_also_supported():
    msg = event_to_inbound(_msg_frame(text="dict body", as_string=False))
    assert msg is not None and msg.text == "dict body"


# ---------------------------------------------------------------- 媒体

def test_voice_is_text_with_transcript_and_no_media_refs():
    frame = _msg_frame(msgtype="voice", body_extra={
        "voice": {"content": "会议室在三楼", "media_id": "m_1"}})
    msg = event_to_inbound(frame)
    assert msg is not None
    assert msg.type is MsgType.TEXT
    assert msg.text == "会议室在三楼"
    assert msg.media_refs == []          # 企微侧已转写，不下载音频


def test_image_media_ref_uses_downloaded_key():
    frame = _msg_frame(msgtype="image", body_extra={
        "image": {"url": "https://cos.example/x.jpg", "aeskey": "BASE64KEY"}})
    items = media_items(frame)
    assert len(items) == 1 and items[0].url.startswith("https://cos")
    msg = event_to_inbound(frame, media_keys=["wecom:key1"])
    assert msg is not None
    assert msg.type is MsgType.IMAGE
    assert [r.storage_key for r in msg.media_refs] == ["wecom:key1"]
    assert msg.media_refs[0].inline_index == 0


def test_media_download_failure_keeps_message_without_refs():
    """单附件失败不移位、不静默丢弃整条消息（multica 陷阱 7）。"""
    frame = _msg_frame(msgtype="file", body_extra={
        "file": {"url": "https://cos.example/a.pdf", "aeskey": "K", "filename": "a.pdf"}})
    msg = event_to_inbound(frame, media_keys=[""])
    assert msg is not None
    assert msg.media_refs == []


def test_mixed_message_joins_text_and_collects_image():
    frame = _msg_frame(msgtype="mixed", body_extra={
        "mixed": {"msg_item": [
            {"msgtype": "text", "text": {"content": "看这个"}},
            {"msgtype": "image", "image": {"url": "https://cos.example/y.png", "aeskey": "K"}},
        ]}})
    items = media_items(frame)
    assert len(items) == 1
    msg = event_to_inbound(frame, media_keys=["wecom:k2"])
    assert msg is not None
    assert msg.text == "看这个"
    assert msg.type is MsgType.TEXT
    assert msg.media_refs[0].storage_key == "wecom:k2"


# ---------------------------------------------------------------- 容错

def test_unknown_msgtype_is_not_silently_dropped():
    frame = _msg_frame(msgtype="location")
    msg = event_to_inbound(frame)
    assert msg is not None                     # 回落 TEXT，下游可回执
    assert msg.type is MsgType.TEXT


def test_missing_msgid_returns_none():
    frame = _msg_frame()
    body = json.loads(frame["body"])
    body.pop("msgid")
    frame["body"] = json.dumps(body)
    assert event_to_inbound(frame) is None


def test_non_message_cmd_returns_none():
    assert event_to_inbound({"cmd": "aibot_subscribe", "body": {}}) is None
    assert event_to_inbound({"cmd": "ping"}) is None


def test_unparsable_body_does_not_raise():
    payload = {"cmd": "aibot_msg_callback", "headers": {"req_id": "r"}, "body": "{bad json"}
    assert event_to_inbound(payload) is None


# ---------------------------------------------------------------- 卡片事件

def test_template_card_event_becomes_card_action():
    msg = template_card_event_to_inbound(_card_event())
    assert msg is not None
    assert msg.type is MsgType.CARD_ACTION
    assert msg.text == "/bind 2"                 # 与「回复 2」同一份命令路径
    assert msg.command_text == "/bind 2"
    assert msg.source.chat_id == "chat_9"
    assert msg.source.sender_id == "lisi"        # 回调带操作人 userid
    assert msg.raw["action_value"]["cmd"] == "/bind 2"


def test_template_card_event_without_route_is_ignored():
    frame = _card_event(chattype="group", chatid="")
    assert template_card_event_to_inbound(frame) is None


def test_card_event_with_unstructured_key_falls_back_to_command():
    msg = template_card_event_to_inbound(_card_event(key="confirm"))
    assert msg is not None and msg.text == "/confirm"


def test_other_events_are_not_card_actions():
    body = {"msgid": "e1", "msgtype": "event", "event": {"eventtype": "enter_chat"},
            "from": {"userid": "u1"}}
    frame = {"cmd": "aibot_event_callback", "headers": {"req_id": "r"},
             "body": json.dumps(body)}
    assert template_card_event_to_inbound(frame) is None