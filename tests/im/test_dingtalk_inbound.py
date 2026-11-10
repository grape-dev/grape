"""钉钉入站 golden 翻译测试（Issue #936 P2，设计文档 §7.6 golden 驱动）。"""
import json

from src.im.adapters.dingtalk.inbound import build_ack_frame, event_to_inbound
from src.im.channel import ChatType, MsgType, Type


def _text_event(msg_id="mid_1", text="hello grape", conv_type="1"):
    return {
        "headers": {
            "contentType": "application/json",
            "messageId": msg_id,
            "eventId": f"evt-{msg_id}",
            "eventType": "chat.message",
            "appKey": "ding_xxx",
        },
        "data": {
            "conversationId": "cid_100",
            "msgId": msg_id,
            "conversationType": conv_type,
            "senderStaffId": "staff_001",
            "senderNick": "张三",
            "msgtype": "text",
            "text": {"content": text},
            "sessionId": "sess_1",
        },
    }


def test_text_message_golden_translation():
    """golden：单聊文本 → 信封（BindingKey=conversation_id）。"""
    m = event_to_inbound(_text_event())
    assert m is not None
    assert m.source.channel_type == Type.DINGTALK
    assert m.source.chat_id == "cid_100"        # BindingKey = conversation_id（§5.2）
    assert m.source.chat_type == ChatType.P2P   # conversationType "1" → 单聊
    assert m.source.sender_id == "staff_001"
    assert m.message_id == "mid_1" and m.event_id == "evt-mid_1"
    assert m.type == MsgType.TEXT and m.text == "hello grape"
    assert m.addressed_to_bot is True
    assert m.command_text == "hello grape"
    assert m.reply_to is not None and m.reply_to.message_id == "sess_1"


def test_group_conversation_type_maps():
    m = event_to_inbound(_text_event(conv_type="2"))
    assert m.source.chat_type == ChatType.GROUP


def test_group_mention_stripped_and_addressed():
    """群内 @ 机器人前缀剥离；剥离后为空 → 未被 @ 裁决（L866 同构）。"""
    m = event_to_inbound(_text_event(text="@机器人 你好", conv_type="2"))
    assert m.text == "你好" and m.addressed_to_bot is True

    m2 = event_to_inbound(_text_event(text="@机器人", conv_type="2"))
    assert m2.text == "" and m2.addressed_to_bot is False


def test_p2p_mention_not_stripped():
    """单聊不剥 @（只群聊才有 @ 裁决语义）。"""
    m = event_to_inbound(_text_event(text="@机器人 hi", conv_type="1"))
    assert m.text == "@机器人 hi"


def test_picture_message_two_hop_media_ref():
    """附件两跳（P4）：picture → MediaRef(downloadCode)，不内联下载/VLM。"""
    ev = _text_event()
    ev["data"]["msgtype"] = "picture"
    ev["data"]["downloadCode"] = "dl_code_1"
    ev["data"]["text"] = {"content": "[图片]"}
    m = event_to_inbound(ev)
    assert m.type == MsgType.IMAGE
    assert len(m.media_refs) == 1
    assert m.media_refs[0].storage_key == "dl_code_1"
    assert m.media_refs[0].type == MsgType.IMAGE


def test_file_message_media_ref():
    ev = _text_event()
    ev["data"]["msgtype"] = "file"
    ev["data"]["downloadCode"] = "dl_file_9"
    m = event_to_inbound(ev)
    assert m.media_refs[0].type == MsgType.FILE


def test_system_event_ignored():
    """SYSTEM 心跳帧不是用户消息。"""
    ev = _text_event()
    ev["headers"]["eventType"] = "SYSTEM"
    assert event_to_inbound(ev) is None


def test_non_message_payload_returns_none():
    assert event_to_inbound({"headers": {}, "data": {}}) is None
    assert event_to_inbound({}) is None
    assert event_to_inbound("not-a-dict") is None


def test_sender_fallback_to_nick():
    ev = _text_event()
    del ev["data"]["senderStaffId"]
    m = event_to_inbound(ev)
    assert m.source.sender_id == "张三"


def test_rich_text_content_fallback():
    ev = _text_event()
    del ev["data"]["text"]
    ev["data"]["content"] = "rich fallback"
    assert event_to_inbound(ev).text == "rich fallback"


def test_ack_frame_contract():
    """ACK 帧契约：code=200 + 原 messageId（3s 时限内回）。"""
    frame = build_ack_frame(_text_event())
    assert frame["code"] == 200
    assert frame["message"] == "OK"
    assert frame["headers"]["messageId"] == "mid_1"
    assert json.loads(frame["data"])["response"] == "OK"
    assert json.dumps(frame)  # 可序列化


def test_data_as_json_string_is_accepted():
    """钉钉协议里 data 是 JSON 字符串（非对象）：不得被静默丢弃（消息黑洞回归）。"""
    ev = _text_event(msg_id="mid_str")
    ev["data"] = json.dumps(ev["data"], ensure_ascii=False)
    ev["type"] = "CALLBACK"
    ev["headers"]["topic"] = "/v1.0/im/bot/messages/get"
    msg = event_to_inbound(ev)
    assert msg is not None
    assert msg.message_id == "mid_str"
    assert msg.text == "hello grape"


def test_malformed_data_string_is_dropped_not_raised():
    ev = _text_event(msg_id="mid_bad")
    ev["data"] = "{not json"
    assert event_to_inbound(ev) is None
