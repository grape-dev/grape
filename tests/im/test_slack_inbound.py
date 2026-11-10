"""Slack 入站翻译测试（Issue #1309 §8.1）——一律用真实 envelope 形态。

覆盖（钉钉/企微两次事故的教训：单测 fixture 全用 dict 抓不到真实形态）：
  - envelope.payload 是 **JSON 字符串**（Socket Mode 实测形态）；
  - `event_id` 重复（Slack 重投）→ 同一幂等键；
  - `bot_message` / `message_changed` / `message_deleted` 显式忽略；
  - 群消息带 `<@Uxxx>` 前缀（app_mention / message 两条路径）；
  - 私聊（D 开头 channel / channel_type=im）自动 addressed；
  - 附件 → MediaRef（只存 url_private_download 引用）；
  - block_actions（value 为 JSON 串 / 纯数字）→ `/bind 2` / `1` 命令，raw 带
    action_value + token（chat.update 原地更新用）。
"""
import json

import pytest

from src.im.adapters.slack.inbound import (
    block_actions_to_inbound,
    chat_type_of,
    event_to_inbound,
    parse_json_maybe,
    strip_mentions,
)
from src.im.channel.types import ChatType, MsgType, Type


def _envelope(event: dict, event_id="Ev123", envelope_id="e-1", string_payload=True):
    payload = {"type": "event_callback", "event_id": event_id, "event": event}
    return {"envelope_id": envelope_id, "type": "events_api",
            "payload": json.dumps(payload, ensure_ascii=False) if string_payload else payload}


def _message_event(**kw):
    base = {
        "type": "message",
        "channel": "C0CHAN",
        "channel_type": "channel",
        "user": "U0USER",
        "text": "<@U0BOT> 你好",
        "ts": "1710000000.000100",
    }
    base.update(kw)
    return base


# ── 真实 envelope / 字符串 payload ──────────────────────────

def test_app_mention_from_string_payload():
    env = _envelope({"type": "app_mention", "channel": "C0CHAN", "user": "U0USER",
                     "text": "<@U0BOT> 帮我看看构建", "ts": "1710000000.000100"})
    msg = event_to_inbound(parse_json_maybe(env["payload"]), envelope_id=env["envelope_id"])
    assert msg is not None
    assert msg.source.channel_type == Type.SLACK
    assert msg.source.chat_id == "C0CHAN"
    assert msg.source.sender_id == "U0USER"
    assert msg.source.chat_type == ChatType.GROUP
    assert msg.text == "帮我看看构建"
    assert msg.command_text == "帮我看看构建"
    assert msg.addressed_to_bot is True
    assert msg.event_id == "Ev123"          # 幂等键 = event_id（不是 ts）
    assert msg.message_id == "1710000000.000100"   # ts 只作线程根/回复目标
    assert msg.type == MsgType.TEXT


def test_message_event_in_thread_sets_thread_root():
    env = _envelope(_message_event(thread_ts="1710000000.000001"))
    msg = event_to_inbound(parse_json_maybe(env["payload"]))
    assert msg.source.thread_root == "1710000000.000001"
    assert msg.reply_to is not None and msg.reply_to.message_id == "1710000000.000100"


def test_p2p_message_addressed_without_mention():
    env = _envelope({"type": "message", "channel": "D0DM", "channel_type": "im",
                     "user": "U0USER", "text": "直接说话", "ts": "1.2"})
    msg = event_to_inbound(parse_json_maybe(env["payload"]))
    assert msg.source.chat_type == ChatType.P2P
    assert msg.addressed_to_bot is True
    assert msg.text == "直接说话"


def test_group_message_without_mention_not_addressed():
    env = _envelope(_message_event(text="大家随便聊"))
    msg = event_to_inbound(parse_json_maybe(env["payload"]))
    assert msg.source.chat_type == ChatType.GROUP
    assert msg.addressed_to_bot is False


def test_multiple_mentions_stripped():
    text, mentioned = strip_mentions("<@U0BOT> 你好 <@U1USER> 再见")
    assert mentioned is True
    assert text == "你好 再见"


def test_chat_type_of_dm_prefix():
    assert chat_type_of({}, "D123") == ChatType.P2P
    assert chat_type_of({"channel_type": "im"}, "C123") == ChatType.P2P
    assert chat_type_of({}, "C123") == ChatType.GROUP


# ── 必须忽略的事件 ─────────────────────────────────────────

@pytest.mark.parametrize("subtype", ["bot_message", "message_changed", "message_deleted"])
def test_bot_and_edit_events_ignored(subtype):
    env = _envelope(_message_event(subtype=subtype))
    assert event_to_inbound(parse_json_maybe(env["payload"])) is None


def test_bot_id_message_ignored():
    env = _envelope(_message_event(bot_id="B0BOT"))
    assert event_to_inbound(parse_json_maybe(env["payload"])) is None


def test_non_message_event_ignored():
    env = _envelope({"type": "reaction_added", "user": "U1", "reaction": "eyes"})
    assert event_to_inbound(parse_json_maybe(env["payload"])) is None


def test_missing_channel_or_ts_returns_none():
    env = _envelope({"type": "message", "user": "U1", "text": "x", "ts": ""})
    assert event_to_inbound(parse_json_maybe(env["payload"])) is None


def test_payload_that_is_not_json_returns_none():
    assert event_to_inbound("{not json") is None
    assert event_to_inbound(None) is None


# ── 附件（只存引用，下载在适配器 api 侧）────────────────────

def test_file_share_builds_media_refs_without_download():
    env = _envelope(_message_event(
        text="", subtype="file_share",
        files=[{"id": "F1", "mimetype": "image/png", "filetype": "png",
                "url_private_download": "https://files.slack.com/files-pri/T-F/x.png"},
               {"id": "F2", "mimetype": "application/pdf", "filetype": "pdf",
                "url_private": "https://files.slack.com/files-pri/T-F/y.pdf"}]))
    msg = event_to_inbound(parse_json_maybe(env["payload"]))
    assert msg is not None
    assert msg.type == MsgType.IMAGE
    assert [r.type for r in msg.media_refs] == [MsgType.IMAGE, MsgType.FILE]
    assert msg.media_refs[0].storage_key.endswith("x.png")
    assert msg.media_refs[1].inline_index == 1
    assert not hasattr(msg.media_refs[0], "data")   # 引用而非内联字节


# ── 卡片回调 ───────────────────────────────────────────────

def _block_actions_payload(action_value, action_id="bind"):
    return {"type": "block_actions",
            "user": {"id": "U0USER"},
            "channel": {"id": "C0CHAN"},
            "message": {"ts": "1710000000.000500",
                        "thread_ts": "1710000000.000001"},
            "actions": [{"action_id": action_id, "value": action_value}],
            "response_url": "https://hooks.slack.com/actions/x"}


def test_block_actions_json_value_becomes_command():
    payload = json.dumps(_block_actions_payload(json.dumps({"cmd": "/bind 2"})))
    msg = block_actions_to_inbound(payload, envelope_id="e-card-1")
    assert msg is not None
    assert msg.type == MsgType.CARD_ACTION
    assert msg.text == "/bind 2"
    assert msg.source.chat_id == "C0CHAN"
    assert msg.source.sender_id == "U0USER"
    assert msg.reply_to is not None and msg.reply_to.message_id == "1710000000.000500"
    # 装配层契约：raw 带 action_value + token（chat.update 用 channel|ts）
    assert msg.raw["action_value"] == {"cmd": "/bind 2"}
    assert msg.raw["token"] == "C0CHAN|1710000000.000500"


def test_block_actions_plain_number_value_falls_back_to_selection():
    payload = _block_actions_payload("2")
    msg = block_actions_to_inbound(payload)
    assert msg is not None and msg.text == "2"


def test_block_actions_action_id_fallback():
    payload = _block_actions_payload("", action_id="help")
    msg = block_actions_to_inbound(payload)
    assert msg is not None and msg.text == "/help"


def test_block_actions_without_message_ts_returns_none():
    payload = {"type": "block_actions", "user": {"id": "U1"},
               "channel": {"id": "C1"}, "actions": [{"action_id": "x", "value": "1"}]}
    assert block_actions_to_inbound(payload) is None


# ── 去重键语义 ─────────────────────────────────────────────

def test_duplicate_event_id_maps_to_same_dedup_key():
    env = _envelope(_message_event(), event_id="Ev-dup", envelope_id="e-a")
    first = event_to_inbound(parse_json_maybe(env["payload"]), envelope_id="e-a")
    env2 = _envelope(_message_event(), event_id="Ev-dup", envelope_id="e-b")
    second = event_to_inbound(parse_json_maybe(env2["payload"]), envelope_id="e-b")
    assert first.event_id == second.event_id == "Ev-dup"


def test_block_actions_dedup_distinguishes_command_and_user():
    """同一张卡上不同按钮 / 不同点击人不得共享 message_id（否则二次点击被去重吞掉）。"""
    a = block_actions_to_inbound(_block_actions_payload(json.dumps({"cmd": "/bind 1"})))
    b = block_actions_to_inbound(_block_actions_payload(json.dumps({"cmd": "/bind 2"})))
    assert a.message_id != b.message_id
    other_user = _block_actions_payload(json.dumps({"cmd": "/bind 1"}))
    other_user["user"] = {"id": "U2OTHER"}
    c = block_actions_to_inbound(other_user)
    assert c.message_id != a.message_id
