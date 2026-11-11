"""微信入站翻译测试（Issue #1728 M1/M2）——真实帧形态 fixture。"""
from src.im.adapters.wechat.inbound import (
    VOICE_NO_TRANSCRIPT,
    dedup_key,
    message_to_inbound,
    messages_to_inbound,
)
from src.im.adapters.wechat.resolvers import decode_media_key
from src.im.channel.types import ChatType, MsgType, Type


def _message(**overrides):
    base = {
        "seq": 1,
        "message_id": 7001,
        "from_user_id": "alice@im.wechat",
        "to_user_id": "bot@im.wechat",
        "message_type": 1,
        "message_state": 2,
        "context_token": "ctx-1",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    }
    base.update(overrides)
    return base


def test_text_message_envelope():
    msg = message_to_inbound(_message(), bot_id="bot-1")
    assert msg is not None
    assert msg.type == MsgType.TEXT
    assert msg.text == "hello"
    assert msg.command_text == "hello"
    assert msg.message_id == "7001"
    assert msg.event_id == "wechat:bot-1:7001"
    assert msg.source.channel_type == Type.WECHAT
    assert msg.source.chat_id == "alice@im.wechat"
    assert msg.source.sender_id == "alice@im.wechat"
    assert msg.source.chat_type == ChatType.P2P
    assert msg.raw["context_token"] == "ctx-1"
    assert msg.addressed_to_bot is True


def test_bot_message_dropped_to_avoid_loop():
    msg = message_to_inbound(_message(message_type=2), bot_id="bot-1")
    assert msg is None


def test_missing_ids_dropped():
    assert message_to_inbound(_message(message_id=0, from_user_id=""), bot_id="b") is None


def test_empty_content_dropped():
    assert message_to_inbound(_message(item_list=[]), bot_id="b") is None


def test_image_media_ref(tmp_path):
    frame = _message(item_list=[{
        "type": 2,
        "image_item": {"aeskey": "00112233445566778899aabbccddeeff",
                       "media": {"full_url": "https://cdn/img", "encrypt_type": 1}},
    }])
    msg = message_to_inbound(frame, bot_id="b")
    assert msg is not None
    assert msg.type == MsgType.IMAGE
    assert msg.text == ""
    assert len(msg.media_refs) == 1
    ref = msg.media_refs[0]
    assert ref.type == MsgType.IMAGE
    media = decode_media_key(ref.storage_key)
    assert media["url"] == "https://cdn/img"
    assert media["aes_key"] == "00112233445566778899aabbccddeeff"


def test_file_item_media_ref():
    frame = _message(item_list=[{
        "type": 4,
        "file_item": {"file_name": "a.pdf",
                      "media": {"encrypt_query_param": "qp", "aes_key": "a2V5"}},
    }])
    msg = message_to_inbound(frame, bot_id="b")
    assert msg.type == MsgType.FILE
    media = decode_media_key(msg.media_refs[0].storage_key)
    assert media["filename"] == "a.pdf"


def test_text_and_media_combined():
    frame = _message(item_list=[
        {"type": 1, "text_item": {"text": "请看图"}},
        {"type": 2, "image_item": {"aeskey": "k", "media": {"full_url": "https://cdn/i"}}},
    ])
    msg = message_to_inbound(frame, bot_id="b")
    assert msg.text == "请看图"
    assert msg.type == MsgType.IMAGE
    assert len(msg.media_refs) == 1


def test_voice_with_transcript_used_as_text():
    frame = _message(item_list=[{
        "type": 3, "voice_item": {"text": "语音转写内容", "media": {"full_url": "https://cdn/v"}},
    }])
    msg = message_to_inbound(frame, bot_id="b")
    assert msg.text == "语音转写内容"
    assert msg.type == MsgType.TEXT


def test_voice_without_transcript_gets_friendly_text():
    frame = _message(item_list=[{"type": 3, "voice_item": {"media": {}}}])
    msg = message_to_inbound(frame, bot_id="b")
    assert msg.text == VOICE_NO_TRANSCRIPT


def test_ref_msg_restored_as_quote_context():
    frame = _message(
        item_list=[{"type": 1, "text_item": {"text": "选 2"}}],
        ref_msg={"message_id": 6988,
                 "item_list": [{"type": 1, "text_item": {"text": "1. repo-a"}},
                               {"type": 1, "text_item": {"text": "2. repo-b"}}]},
    )
    msg = message_to_inbound(frame, bot_id="b")
    assert msg.reply_to.message_id == "6988"
    assert "repo-b" in msg.raw["ref_text"]


def test_dedup_key_shape():
    assert dedup_key("bot", "1") == "wechat:bot:1"


def test_messages_to_inbound_filters_bot_messages():
    frames = [_message(message_id=1), _message(message_id=2, message_type=2)]
    out = messages_to_inbound(frames, bot_id="b")
    assert [m.message_id for m in out] == ["1"]