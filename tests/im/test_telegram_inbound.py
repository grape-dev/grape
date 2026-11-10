"""Telegram 入站翻译测试（Issue #1613 批 1/2/3）。

覆盖：
  - 单聊/群聊文本映射、群内剥 @botname、privacy mode 的 addressed 裁决；
  - `edited_message` / 空消息忽略；媒体取最大 photo、document 类型、20MB 超限标记；
  - `callback_query` 短码 `b:2` → `/bind 2`；超长参数走 cid 台账回查。
"""
from src.im.adapters.telegram.cards import TelegramCardLedger
from src.im.adapters.telegram.inbound import (
    callback_query_to_inbound,
    strip_bot_mention,
    update_to_inbound,
)
from src.im.channel.types import ChatType, MsgType, Type


def _message(text="hello", chat_type="private", chat_id=123, sender=42, **extra):
    message = {
        "message_id": 5,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender, "first_name": "Alice", "last_name": "A"},
        "text": text,
    }
    message.update(extra)
    return {"update_id": 100, "message": message}


def test_private_text_maps_to_p2p_envelope():
    msg = update_to_inbound(_message("/help"))
    assert msg is not None
    assert msg.source.channel_type == Type.TELEGRAM
    assert msg.source.chat_id == "123"
    assert msg.source.chat_type == ChatType.P2P
    assert msg.source.sender_id == "42"
    assert msg.message_id == "5"
    assert msg.event_id == "100"
    assert msg.type == MsgType.TEXT
    assert msg.text == "/help" and msg.command_text == "/help"
    assert msg.addressed_to_bot is True
    assert msg.reply_to.message_id == "5"


def test_group_text_strips_bot_mention_and_requires_address():
    msg = update_to_inbound(_message("@grape_bot 你好", chat_type="supergroup",
                                     chat_id=-100), bot_username="grape_bot")
    assert msg.text == "你好"
    assert msg.source.chat_type == ChatType.GROUP
    assert msg.addressed_to_bot is True

    plain = update_to_inbound(_message("普通聊天", chat_type="group", chat_id=-100),
                              bot_username="grape_bot")
    assert plain.text == "普通聊天"
    assert plain.addressed_to_bot is False


def test_group_command_is_addressed_without_mention():
    msg = update_to_inbound(_message("/repo", chat_type="group", chat_id=-100),
                            bot_username="grape_bot")
    assert msg.addressed_to_bot is True


def test_strip_bot_mention_does_not_touch_middle_text():
    text, mentioned = strip_bot_mention("请看 @alice 的报告", bot_username="grape_bot")
    assert mentioned is False
    assert text == "请看 @alice 的报告"


def test_edited_message_and_empty_message_ignored():
    edited = {"update_id": 101, "edited_message": {"message_id": 1}}
    assert update_to_inbound(edited) is None
    empty = {"update_id": 102, "message": {"message_id": 1, "chat": {"id": 1}}}
    assert update_to_inbound(empty) is None


def test_photo_uses_largest_size_as_media_ref():
    msg = update_to_inbound(_message("", photo=[
        {"file_id": "small", "file_size": 100},
        {"file_id": "big", "file_size": 500},
    ]))
    assert msg.type == MsgType.IMAGE
    assert [ref.storage_key for ref in msg.media_refs] == ["big"]
    assert msg.media_refs[0].type == MsgType.IMAGE


def test_document_image_and_file_classification():
    photo_doc = update_to_inbound(_message("", document={
        "file_id": "doc-img", "file_name": "a.PNG", "file_size": 10}))
    assert photo_doc.type == MsgType.IMAGE
    pdf = update_to_inbound(_message("", document={
        "file_id": "doc-pdf", "file_name": "a.pdf", "file_size": 10}))
    assert pdf.type == MsgType.FILE


def test_media_over_20mb_is_flagged():
    update = _message("", document={
        "file_id": "huge", "file_name": "big.zip",
        "file_size": 21 * 1024 * 1024})
    msg = update_to_inbound(update)
    assert msg.raw.get("media_too_large") is True


def test_callback_short_code_decodes_to_command():
    callback = {
        "id": "cq-1",
        "from": {"id": 42, "first_name": "Alice"},
        "data": "b:2",
        "message": {"message_id": 7, "chat": {"id": 123, "type": "private"}},
    }
    msg = callback_query_to_inbound(callback)
    assert msg is not None
    assert msg.type == MsgType.CARD_ACTION
    assert msg.text == "/bind 2"
    assert msg.raw["action_value"] == {"cmd": "/bind 2"}
    assert msg.raw["token"] == "123|7"
    assert msg.raw["callback_query_id"] == "cq-1"
    assert msg.event_id == "cb:cq-1"


def test_callback_cid_looks_up_ledger():
    ledger = TelegramCardLedger()
    payload = {"cmd": "/finding-dispatch", "exec_id": 12, "finding_id": "34",
               "scene_id": "6e2dba56-1c39-4885-8598-d4d7fd089a0f"}
    cid = ledger.put(payload)
    callback = {
        "id": "cq-2", "from": {"id": 42}, "data": f"c:{cid}",
        "message": {"message_id": 8, "chat": {"id": 123, "type": "private"}},
    }
    msg = callback_query_to_inbound(callback, ledger=ledger)
    assert msg.raw["action_value"] == payload
    assert msg.text == "/finding-dispatch"


def test_callback_unknown_data_returns_none():
    callback = {
        "id": "cq-3", "from": {"id": 42}, "data": "unknown-short-code",
        "message": {"message_id": 8, "chat": {"id": 123}},
    }
    assert callback_query_to_inbound(callback) is None