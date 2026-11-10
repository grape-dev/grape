"""钉钉互动卡片（批 5.1）：回调解析 + 订阅 + 能力位 + 实例客户端载荷。

这些用例守住三件事：
  1. 卡片按钮回调 → 等价命令（/bind N、/cancel），chat 由按钮 params 带回；
  2. CARD_ACTION 只在 interactive 模式申报（默认不虚报）；
  3. 卡片实例 API 载荷形状（create / deliver / update）。
真实投放/回调需真机（模板 + 权限），见 docs/weekly-09/im-dingtalk-interactive-card-batch5.md。
"""
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.im.adapters.dingtalk import cards as cards_mod
from src.im.adapters.dingtalk.channel import _subscriptions
from src.im.adapters.dingtalk.inbound import (
    CARD_CALLBACK_TOPIC,
    card_callback_to_inbound,
    event_to_inbound,
)
from src.im.adapters.dingtalk.outbound import DingtalkSender
from src.im.channel.capability import Capability


def _frame(params: dict, *, chat_in_content: bool = False, typ: str = "actionCallback"):
    content = {
        "type": typ,
        "outTrackId": "grape-inst-abc123",
        "userId": "staff_1",
        "cardPrivateData": {"actionIds": ["btn_2"], "params": params},
    }
    data = {
        "type": "actionCallback",
        "outTrackId": "grape-inst-abc123",
        "userId": "staff_1",
        "content": json.dumps(content),        # content 是 JSON 字符串（同 data 坑）
    }
    if chat_in_content:
        content["conversationId"] = "cid_from_content"
        data["content"] = json.dumps(content)
    return {"headers": {"topic": CARD_CALLBACK_TOPIC, "messageId": "m1"},
            "data": json.dumps(data)}          # data 也是 JSON 字符串


def test_card_callback_becomes_bind_command():
    msg = card_callback_to_inbound(_frame(
        {"action": "select", "cmd": "bind", "num": 2, "chat": "cidYkV5"}))
    assert msg is not None
    assert msg.command_text == "/bind 2"
    assert msg.source.chat_id == "cidYkV5"
    assert msg.source.sender_id == "staff_1"
    assert msg.addressed_to_bot is True
    assert msg.message_id.startswith("card:grape-inst-abc123:btn_2:/bind 2")


def test_card_callback_cancel():
    msg = card_callback_to_inbound(_frame({"action": "cancel", "chat": "cidYkV5"}))
    assert msg is not None and msg.command_text == "/cancel"


def test_card_callback_page():
    msg = card_callback_to_inbound(_frame(
        {"action": "page", "cmd": "repo", "page": 2, "chat": "cidYkV5"}))
    assert msg is not None and msg.command_text == "/repo 2"


def test_card_callback_without_chat_is_ignored():
    """回调不带 chat 参数 = 不是我们投放的卡，宁可忽略也不猜会话。"""
    assert card_callback_to_inbound(_frame({"action": "select", "cmd": "bind", "num": 1})) is None
    # 回传体里即使有 conversationId 也不能当 chat（未文档化，容易猜错）
    assert card_callback_to_inbound(_frame(
        {"action": "select", "cmd": "bind", "num": 1}, chat_in_content=True)) is None


def test_card_callback_other_frames_untouched():
    assert card_callback_to_inbound({"headers": {"topic": "/v1.0/im/bot/messages/get"}}) is None
    assert card_callback_to_inbound(_frame({"action": "nope", "chat": "cid"})) is None
    assert card_callback_to_inbound(_frame({"action": "select", "cmd": "bind", "num": 1},
                                           typ="otherCallback")) is None


def test_card_frame_not_parsed_as_chat_message():
    """卡片回调帧不能被 event_to_inbound 当成聊天消息（否则会当无关文本丢进 agent）。"""
    assert event_to_inbound(_frame({"action": "select", "cmd": "bind", "num": 1,
                                    "chat": "cidYkV5"})) is None


def test_subscriptions_include_card_callback_only_when_enabled(monkeypatch):
    from src.im.adapters.dingtalk import channel as ch
    # 默认 off：不动消息通道（未开权限的 app 带卡片订阅有被拒风险）
    monkeypatch.setattr(ch, "_card_switch_on", lambda: False)
    topics = [s.get("topic") for s in ch._subscriptions()]
    assert CARD_CALLBACK_TOPIC not in topics
    assert "/v1.0/im/bot/messages/get" in topics

    monkeypatch.setattr(ch, "_card_switch_on", lambda: True)
    topics = [s.get("topic") for s in ch._subscriptions()]
    assert CARD_CALLBACK_TOPIC in topics
    assert "/v1.0/im/bot/messages/get" in topics


def test_capability_card_action_only_in_interactive_mode():
    base = DingtalkSender(lambda *a, **k: "m", lambda *a, **k: "m",
                          lambda *a, **k: "m")
    assert not (base.capabilities() & Capability.CARD_ACTION)   # 默认不虚报

    inter = DingtalkSender(lambda *a, **k: "m", lambda *a, **k: "m",
                           lambda *a, **k: "m", card_mode="interactive")
    caps = inter.capabilities()
    assert caps & Capability.CARD_ACTION
    assert caps & Capability.RICH_CARD           # 渲染能力仍在
    assert caps & Capability.TEXT


class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.content = b"{}"
        self._body = body or {}

    def json(self):
        return self._body


class _FakeClient:
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, json=None, headers=None):
        _FakeClient.calls.append({"method": method, "url": url, "json": json,
                                  "headers": headers})
        return _FakeResp()


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(cards_mod.httpx, "AsyncClient", _FakeClient)

    async def _token():
        return "tok_123"

    return lambda: cards_mod.DingtalkCardClient("app", "sec", token_provider=_token)


def test_create_and_deliver_payload_robot(fake_http):
    """单聊：一次 createAndDeliver，场域 IM_ROBOT.<staffId>（官方 SDK 口径）。"""
    import asyncio
    client = fake_http()
    ok, _ = asyncio.run(client.create_and_deliver(
        "tpl_select", "track_1", {"title": "选择仓库", "body_md": "a\nb", "num": 3},
        conversation_id="cidYkV5", conversation_type="1", staff_id="staff_1"))
    assert ok
    call = _FakeClient.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/v1.0/card/instances/createAndDeliver")
    assert call["headers"]["x-acs-dingtalk-access-token"] == "tok_123"
    body = call["json"]
    assert body["cardTemplateId"] == "tpl_select"
    assert body["outTrackId"] == "track_1"
    assert body["callbackType"] == "STREAM"          # 回调走长连接
    # 非字符串值必须序列化成字符串（cardParamMap 只收 String）
    assert body["cardData"]["cardParamMap"] == {"title": "选择仓库", "body_md": "a\nb",
                                               "num": "3"}
    assert body["openSpaceId"] == "dtv1.card//IM_ROBOT.staff_1"
    assert body["imRobotOpenDeliverModel"] == {"spaceType": "IM_ROBOT"}


def test_create_and_deliver_payload_group(fake_http):
    """群聊：场域 IM_GROUP.<conversationId> + robotCode。"""
    import asyncio
    client = fake_http()
    ok, _ = asyncio.run(client.create_and_deliver(
        "tpl", "track_2", {"title": "t"},
        conversation_id="cidGROUP", conversation_type="2", robot_code="ding_app"))
    assert ok
    body = _FakeClient.calls[0]["json"]
    assert body["openSpaceId"] == "dtv1.card//IM_GROUP.cidGROUP"
    assert body["imGroupOpenDeliverModel"] == {"robotCode": "ding_app"}


def test_update_payload(fake_http):
    import asyncio
    client = fake_http()
    ok, _ = asyncio.run(client.update("track_1", {"title": "已绑定", "flag": True}))
    assert ok
    call = _FakeClient.calls[0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/v1.0/card/instances")
    assert call["json"]["cardData"]["cardParamMap"] == {"title": "已绑定", "flag": "true"}
    assert call["json"]["cardUpdateOptions"] == {"updateCardDataByKey": True}


def test_deliver_payload(fake_http):
    import asyncio
    client = fake_http()
    ok, _ = asyncio.run(client.deliver("track_1", conversation_id="cid1",
                                       conversation_type="1", staff_id="staff_9"))
    assert ok
    body = _FakeClient.calls[0]["json"]
    assert body["outTrackId"] == "track_1" and body["userIdType"] == 1
    assert body["openSpaceId"] == "dtv1.card//IM_ROBOT.staff_9"


def test_create_and_deliver_stops_on_failure(monkeypatch):
    import asyncio
    client = cards_mod.DingtalkCardClient("app", "sec", token_provider=lambda: "t")

    async def _bad(*a, **kw):
        return False, {"error": "403 no permission"}

    monkeypatch.setattr(client, "_request", _bad)
    ok, data = asyncio.run(client.create_and_deliver(
        "tpl", "track", {}, conversation_id="cid", staff_id="u"))
    assert ok is False and "403" in str(data)

def test_card_callback_num_only_becomes_digit_command():
    """通用模板按钮只带 num（params 固定，无法带 cmd/chat）→ 输出纯数字，
    走引擎既有 pending 选择分支；chat 由 outTrackId 映射反查。"""
    fr = _frame({"action": "select", "num": 3})
    assert card_callback_to_inbound(fr) is None            # 没映射不猜
    msg = card_callback_to_inbound(fr, chat_resolver=lambda tid: "cidRESOLVED")
    assert msg is not None
    assert msg.command_text == "3"
    assert msg.source.chat_id == "cidRESOLVED"


def test_card_callback_resolver_not_used_when_chat_present():
    msg = card_callback_to_inbound(
        _frame({"action": "select", "num": 1, "chat": "cidFromParams"}),
        chat_resolver=lambda tid: "cidWRONG")
    assert msg is not None and msg.source.chat_id == "cidFromParams"


def test_stringify_params_serializes_non_strings():
    assert cards_mod.stringify_params({"a": 1, "b": True, "c": "x", "d": {"k": 1}}) == {
        "a": "1", "b": "true", "c": "x", "d": '{"k": 1}'}


# ---------------------------------------------------------------- outTrackId 台账

def test_outbound_card_cache_then_db_fallback(monkeypatch):
    """发送时写入 → 内存命中；清缓存后回落 DB（模拟进程重启后点旧卡）。"""
    from src.im.engine import outbound_cards as oc
    oc._clear_cache()
    monkeypatch.setattr(oc, "_save_sync", lambda *a, **k: None)
    store = {}

    def _fake_load(tid):
        return store.get(tid)

    monkeypatch.setattr(oc, "_load_sync", _fake_load)
    oc.remember("track_1", chat_id="cidYkV5", chat_type="1", sender_id="staff_1",
                installation_id="inst_1", template_id="tpl")
    assert oc.lookup_chat("track_1") == "cidYkV5"       # 内存
    oc._clear_cache()
    store["track_1"] = {"chat_id": "cidYkV5"}
    assert oc.lookup_chat("track_1") == "cidYkV5"       # DB 回落
    assert oc.lookup_chat("nope") == ""
    assert oc.lookup_chat("") == ""


def test_outbound_card_persist_failure_does_not_raise(monkeypatch):
    """台账写失败只告警，不允许影响发送。"""
    from src.im.engine import outbound_cards as oc
    oc._clear_cache()

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(oc, "_save_sync", _boom)
    oc.remember("track_2", chat_id="cid")               # 不抛
    assert oc.lookup_chat("track_2") == "cid"


def test_channel_outbound_card_table_exists(tmp_path):
    """迁移脚本必须建出 grape_t_channel_outbound_card（MySQL 依赖同一条 CREATE IF NOT EXISTS）。"""
    import importlib.util
    from pathlib import Path
    import sqlite3
    mig = Path(__file__).resolve().parents[2] / "scripts" / "migrations" / "001_channel_tables.py"
    spec = importlib.util.spec_from_file_location("mig001_test", mig)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    conn = sqlite3.connect(tmp_path / "t.db")
    mod.ensure_channel_tables(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(grape_t_channel_outbound_card)")]
    assert {"out_track_id", "installation_id", "channel_chat_id", "chat_type",
            "sender_staff_id", "card_template_id"} <= set(cols)
    conn.execute("INSERT INTO grape_t_channel_outbound_card (out_track_id, installation_id, "
                 "channel_chat_id, chat_type) VALUES ('t1','i1','cid','1')")
    conn.commit()
    assert conn.execute("SELECT channel_chat_id FROM grape_t_channel_outbound_card "
                        "WHERE out_track_id='t1'").fetchone()[0] == "cid"


def test_out_track_id_of_frame():
    from src.im.adapters.dingtalk.inbound import out_track_id_of
    assert out_track_id_of(_frame({"action": "cancel", "chat": "c"})) == "grape-inst-abc123"
    assert out_track_id_of({"headers": {}}) == ""


# ---------------------------------------------------------------- interactive 发送路径

class _StubCardClient:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def create_and_deliver(self, template_id, out_track_id, card_data, **kw):
        self.calls.append({"template_id": template_id, "out_track_id": out_track_id,
                           "card_data": card_data, **kw})
        return (self.ok, {} if self.ok else {"error": "403"})


def _register_chat(chat, staff_id="staff_1", chat_type="1"):
    from src.im.adapters.dingtalk.outbound import default_webhook_registry
    default_webhook_registry().register(chat, "https://wh", staff_id=staff_id,
                                        chat_type=chat_type)


def test_interactive_mode_sends_template_and_remembers(monkeypatch):
    import asyncio
    from src.im.adapters.dingtalk.outbound import DingtalkSender
    from src.im.engine import outbound_cards as oc
    from src.im.channel.message import OutboundMessage
    oc._clear_cache()
    _register_chat("cid_inter_1")
    client = _StubCardClient()
    sender = DingtalkSender(lambda *a, **k: "t", lambda *a, **k: "m",
                            lambda *a, **k: "ac", card_mode="interactive",
                            card_template_id="tpl_1", card_client=client)
    out = OutboundMessage(chat_id="cid_inter_1", text="**📁 选择仓库**\n1. a\n2. b",
                          metadata={"kind": "card", "title": "选择仓库"})
    res = asyncio.run(sender.send(out))
    assert res.ok
    call = client.calls[0]
    assert call["template_id"] == "tpl_1"
    assert call["conversation_id"] == "cid_inter_1"
    assert call["conversation_type"] == "1" and call["staff_id"] == "staff_1"
    # 换行要带硬断行（markdown 单 \n 不换行）
    assert call["card_data"]["body_md"] == "**📁 选择仓库**  \n1. a  \n2. b"
    assert call["card_data"]["title"] == "选择仓库"
    # 台账写入了 → 回调能反查到会话
    assert oc.lookup_chat(call["out_track_id"]) == "cid_inter_1"


def test_interactive_failure_falls_back_to_action_card(monkeypatch):
    import asyncio
    from src.im.adapters.dingtalk.outbound import DingtalkSender
    from src.im.channel.message import OutboundMessage
    _register_chat("cid_inter_2")
    calls = []
    async def _ac(chat_id, title, text, btns=None, **kw):
        calls.append(("card", title, text, list(btns or [])))
        return "ac_mid"
    client = _StubCardClient(ok=False)
    sender = DingtalkSender(lambda *a, **k: "t", lambda *a, **k: "m", _ac,
                            card_mode="interactive", card_template_id="tpl_1",
                            card_client=client)
    out = OutboundMessage(chat_id="cid_inter_2", text="hi",
                          metadata={"kind": "card", "title": "T"})
    res = asyncio.run(sender.send(out))
    assert res.ok and res.message_id == "ac_mid"       # 回落没坏
    assert calls and calls[0][0] == "card"


def test_interactive_skipped_without_staff_id(monkeypatch):
    """单聊没有 staff_id（webhook 未登记）→ 不硬发，交给回落路径。"""
    import asyncio
    from src.im.adapters.dingtalk.outbound import DingtalkSender
    from src.im.channel.message import OutboundMessage
    client = _StubCardClient()
    ac = []
    async def _ac(chat_id, title, text, btns=None, **kw):
        ac.append(1)
        return "ac_mid"
    sender = DingtalkSender(lambda *a, **k: "t", lambda *a, **k: "m", _ac,
                            card_mode="interactive", card_template_id="tpl_1",
                            card_client=client)
    out = OutboundMessage(chat_id="cid_never_registered", text="hi",
                          metadata={"kind": "card"})
    res = asyncio.run(sender.send(out))
    assert client.calls == [] and ac            # 没发卡片实例，走了回落
    assert res.ok


def test_interactive_does_not_touch_plain_text():
    import asyncio
    from src.im.adapters.dingtalk.outbound import DingtalkSender
    from src.im.channel.message import OutboundMessage
    client = _StubCardClient()
    texts = []
    async def _t(chat_id, text):
        texts.append(text); return "mid"
    sender = DingtalkSender(_t, _t, lambda *a, **k: "ac", card_mode="interactive",
                            card_template_id="tpl_1", card_client=client)
    res = asyncio.run(sender.send(OutboundMessage(chat_id="cid_x", text="普通一句话")))
    assert res.ok and client.calls == [] and texts == ["普通一句话"]


# ---------------------------------------------------------------- 模式决策 / 状态视图

def test_dingtalk_card_mode_decision(monkeypatch, tmp_path):
    """三件齐才 interactive：模板 + 安装 enabled + 全局开关；缺一律回落。"""
    from src.im import engine_wiring as ew
    switch = tmp_path / "card_mode"
    monkeypatch.setenv("GRAPE_DINGTALK_CARD_MODE_FILE", str(switch))
    monkeypatch.delenv("GRAPE_DINGTALK_CARD_MODE", raising=False)

    cfg = {"dingtalk_cards": {"card": "tpl_1"}, "cards_enabled": True}
    assert ew.dingtalk_card_mode(cfg) == ("action_card", "tpl_1")   # 全局开关 off

    switch.write_text("interactive", encoding="utf-8")
    assert ew.dingtalk_card_mode(cfg) == ("interactive", "tpl_1")
    assert ew.dingtalk_card_mode({"dingtalk_cards": {"card": "tpl_1"}}) == ("action_card", "tpl_1")
    assert ew.dingtalk_card_mode({}) == ("action_card", "")
    assert ew.dingtalk_card_mode({"dingtalk_cards": "bad"}) == ("action_card", "")


def test_card_setup_view():
    from src.api.routes.im import _card_setup_view
    v = _card_setup_view("inst_1", {"dingtalk_cards": {"card": "tpl"}, "cards_enabled": True})
    assert v["mode"] == "interactive" and v["configured"] and v["enabled"]
    v = _card_setup_view("inst_1", {"dingtalk_cards": {"card": "tpl"}})
    assert v["mode"] == "action_card" and v["configured"] and not v["enabled"]
    v = _card_setup_view("inst_1", {})
    assert v["mode"] == "action_card" and not v["configured"]
