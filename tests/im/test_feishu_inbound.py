"""飞书入站翻译测试（Issue #742 §7.6 golden 驱动 + P0 双写对比基线）。"""
import pytest

from src.im.adapters.feishu.inbound import parse_card_action, parsed_to_inbound
from src.im.channel import ChatType, MsgType, Type


def test_text_message_translation():
    parsed = {
        "chat_id": "oc_abc", "chat_type": "p2p", "sender_open_id": "ou_1",
        "message_id": "om_1", "msg_type": "text", "text": "hello grape",
        "event_id": "evt_1",
    }
    m = parsed_to_inbound(parsed, raw_event={"header": {"event_id": "evt_1"}})
    assert m.source.chat_id == "oc_abc" and m.source.chat_type == ChatType.P2P
    assert m.source.sender_id == "ou_1" and m.source.channel_type == Type.FEISHU
    assert m.message_id == "om_1" and m.event_id == "evt_1"
    assert m.type == MsgType.TEXT and m.text == "hello grape"
    assert m.addressed_to_bot is True
    assert m.raw.get("header", {}).get("event_id") == "evt_1"


def test_group_mention_stripped_empty_not_addressed():
    """群内 @ 剥离后为空 → addressed_to_bot=False（L866 语义）。"""
    parsed = {"chat_id": "oc_g", "chat_type": "group", "sender_open_id": "ou_2",
              "message_id": "om_2", "msg_type": "text", "text": ""}
    m = parsed_to_inbound(parsed)
    assert m.source.chat_type == ChatType.GROUP
    assert m.addressed_to_bot is False


def test_group_with_text_addressed():
    parsed = {"chat_id": "oc_g", "chat_type": "group", "sender_open_id": "ou_2",
              "message_id": "om_3", "msg_type": "text", "text": "@bot 你好"}
    assert parsed_to_inbound(parsed).addressed_to_bot is True


def test_image_message_becomes_media_ref():
    """P4：图片消息 → MediaRef 引用，不内联（L1074 image_key 语义）。"""
    parsed = {"chat_id": "oc_1", "chat_type": "p2p", "sender_open_id": "ou_1",
              "message_id": "om_4", "msg_type": "image", "text": "[图片]",
              "image_key": "img_v2_xyz"}
    m = parsed_to_inbound(parsed)
    assert m.type == MsgType.IMAGE
    assert len(m.media_refs) == 1
    assert m.media_refs[0].storage_key == "img_v2_xyz"
    assert m.text == "[图片]"


def test_media_message_becomes_media_ref():
    parsed = {"chat_id": "oc_1", "chat_type": "p2p", "sender_open_id": "ou_1",
              "message_id": "om_5", "msg_type": "media", "text": "[视频]",
              "file_key": "file_v2_vid"}
    m = parsed_to_inbound(parsed)
    assert m.media_refs[0].storage_key == "file_v2_vid"
    assert m.media_refs[0].type == MsgType.MEDIA


def test_command_text_not_richified():
    """command_text 保留富化前原文（防 /issue 二次解释）。"""
    parsed = {"chat_id": "oc_1", "chat_type": "p2p", "sender_open_id": "ou_1",
              "message_id": "om_6", "msg_type": "text",
              "text": "/repo owner/name", "command_text": "/repo owner/name"}
    m = parsed_to_inbound(parsed)
    assert m.command_text == "/repo owner/name"


def test_card_action_trigger_translation():
    """card.action.trigger（原 _parse_card_action L911 语义迁移）。"""
    raw = {"event": {
        "action": {"value": {"selection": "2"}},
        "chat_id": "oc_g", "message_id": "om_c",
        "operator": {"open_id": "ou_op"},
    }}
    act = parse_card_action(raw)
    assert act["action_value"] == {"selection": "2"}
    assert act["chat_id"] == "oc_g" and act["open_id"] == "ou_op"
    assert parse_card_action({"event": {"action": {}}}) is None
