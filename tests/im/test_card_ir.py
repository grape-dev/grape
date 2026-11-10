"""Card IR 契约测试（Issue #1095）。

验收对照：
  1. 同一 IR fixture × 3 renderer：飞书 JSON 与迁移前等价（快照零漂移）、
     钉钉无空按钮、文本渲染保留全部可见文本；
  2. 空内容硬护栏（egress / DingtalkSender）；
  3. 能力位断言 + 启动自检；
  4. /repo /chat /task /help + autopilot 链路 IR 构造完整性（mock 驱动）；
  6. degraded_by_reason 指标；
  静态检查：业务层禁止 lark_md / behaviors / template 平台字段。
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.im.channel.capability import Capability, describe
from src.im.channel.card import (
    CARD_IR_ENV,
    Action,
    Block,
    ButtonStyle,
    Card,
    Tone,
    action_payload,
    button_block,
    card_ir_enabled,
    divider_block,
    render_text,
    row_block,
    select_block,
    self_check_card_capabilities,
    simple_card,
    text_block,
    validate_card_capability,
)
from src.im.channel.renderers.dingtalk import DingtalkCardRenderer
from src.im.channel.renderers.feishu import FeishuCardRenderer
from src.im.engine.egress import HandlerEgress
from src.im.engine.metrics import DEGRADED_REASONS, EngineMetrics

# ---------------------------------------------------------------- fixtures

def _repo_card_ir() -> Card:
    """选仓库卡 IR（对应 handler_async._send_repo_card 迁移前形态）。"""
    items = ["owner/a（owner）", "owner/b（member）"]
    blocks = [text_block("列表文本")]
    for i in range(0, len(items), 3):
        blocks.append(row_block(*[
            Action(id="bind", label=str(j + 1), params={"num": j + 1})
            for j in range(i, min(i + 3, len(items)))]))
    blocks.append(row_block(Action(id="help", label="❓ 帮助")))
    return Card(title="📁 选择仓库", tone=Tone.DEFAULT, blocks=tuple(blocks))


def _autopilot_list_ir() -> Card:
    """autopilot list 态卡 IR（v2 内联按钮 + 下拉）。"""
    return Card(
        title="任务产出 · demo",
        tone=Tone.DEFAULT,
        blocks=(
            text_block("**待派发问题**\n🔴 **1. finding one**"),
            divider_block(),
            select_block("选择要派发的问题", [
                Action(id="finding-select", label="1. finding one",
                       params={"exec_id": 7, "finding_id": "f1"}),
            ]),
            divider_block(),
            text_block("· 选择问题后进入场景选择，确认后派发为 Issue"),
        ),
        form="v2",
    )


REPO_CARD_SNAPSHOT = {
    "header": {"title": {"tag": "plain_text", "content": "📁 选择仓库"},
               "template": "blue"},
    "elements": [
        {"tag": "div", "text": {"tag": "lark_md", "content": "列表文本"}},
        {"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "1"},
             "value": {"cmd": "/bind 1"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "2"},
             "value": {"cmd": "/bind 2"}},
        ]},
        {"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "❓ 帮助"},
             "value": {"cmd": "/help"}},
        ]},
    ],
}


# ---------------------------------------------------------------- 动作语义

def test_action_payload_positional_num_folds_into_cmd():
    assert action_payload(Action(id="bind", params={"num": 3})) == {"cmd": "/bind 3"}


def test_action_payload_params_preserved():
    act = Action(id="finding-dispatch", params={"exec_id": 7, "finding_id": "f1",
                                                "scene_id": "s1"})
    assert action_payload(act) == {"cmd": "/finding-dispatch", "exec_id": 7,
                                   "finding_id": "f1", "scene_id": "s1"}


def test_action_payload_plain_command():
    assert action_payload(Action(id="help")) == {"cmd": "/help"}


# ---------------------------------------------------------------- 飞书 renderer（外观零漂移）

def test_feishu_repo_card_matches_pre_migration_snapshot():
    """迁移前后 JSON 等价（字段序无关）：选仓库卡快照断言。"""
    rendered = FeishuCardRenderer().render(_repo_card_ir()).payload
    assert rendered == REPO_CARD_SNAPSHOT


def test_feishu_v2_autopilot_card_shape():
    out = FeishuCardRenderer().render(_autopilot_list_ir()).payload
    assert out["schema"] == "2.0"
    assert out["header"]["template"] == "blue"
    tags = [el["tag"] for el in out["body"]["elements"]]
    assert tags == ["div", "hr", "select_static", "hr", "div"]
    sel = out["body"]["elements"][2]
    assert sel["placeholder"]["content"] == "选择要派发的问题"
    value = json.loads(sel["options"][0]["value"])
    assert value == {"cmd": "/finding-select", "exec_id": 7, "finding_id": "f1"}


def test_feishu_v2_primary_button_has_type():
    card = Card(title="t", tone=Tone.SUCCESS, form="v2", blocks=(
        button_block(Action(id="finding-dispatch", label="确认派发",
                            style=ButtonStyle.PRIMARY, params={"exec_id": 1})),
        button_block(Action(id="finding-list", label="返回问题列表",
                            params={"exec_id": 1})),
    ))
    els = FeishuCardRenderer().render(card).payload["body"]["elements"]
    assert els[0].get("type") == "primary"
    assert "type" not in els[1]
    assert els[0]["behaviors"][0]["type"] == "callback"
    assert els[0]["behaviors"][0]["value"] == {"cmd": "/finding-dispatch", "exec_id": 1}


def test_feishu_tone_mapping():
    assert FeishuCardRenderer().render(Card(title="a", tone=Tone.DANGER)).payload["header"]["template"] == "red"
    assert FeishuCardRenderer().render(Card(title="a", tone=Tone.SUCCESS)).payload["header"]["template"] == "green"
    assert FeishuCardRenderer().render(Card(title="a", tone=Tone.NEUTRAL)).payload["header"]["template"] == "grey"
    assert FeishuCardRenderer().render(Card(title="a", tone=Tone.WARNING)).payload["header"]["template"] == "yellow"


def test_feishu_button_danger_style_mapped():
    card = simple_card("t", "b", Tone.DEFAULT,
                       Action(id="danger", label="删除", style=ButtonStyle.DANGER))
    els = FeishuCardRenderer().render(card).payload["elements"]
    assert els[-1]["actions"][0]["type"] == "danger"
    v2 = Card(title="t", form="v2", blocks=(
        button_block(Action(id="danger", label="删除", style=ButtonStyle.DANGER)),))
    body = FeishuCardRenderer().render(v2).payload["body"]["elements"]
    assert body[0]["type"] == "danger"


def test_feishu_v1_bottom_actions_row():
    card = simple_card("✅ 绑定成功", "正文", Tone.SUCCESS,
                       Action(id="chat", label="📋 选择会话"))
    out = FeishuCardRenderer().render(card).payload
    row = out["elements"][-1]
    assert row["tag"] == "action"
    assert row["actions"][0]["value"] == {"cmd": "/chat"}


def test_feishu_renderer_never_emits_empty_elements_key():
    card = Card(title="t", tone=Tone.DEFAULT, blocks=(divider_block(),))
    out = FeishuCardRenderer().render(card).payload
    assert out["elements"] == [{"tag": "hr"}]


def test_feishu_fields_block():
    card = Card(title="t", blocks=(Block(kind="fields", fields=(("k", "v"),)),))
    out = FeishuCardRenderer().render(card).payload
    assert out["elements"][0]["tag"] == "div"
    assert out["elements"][0]["fields"][0]["text"]["content"] == "**k**\nv"


# ---------------------------------------------------------------- 钉钉 renderer

def test_dingtalk_action_card_basic():
    card = simple_card("标题", "正文", Tone.DEFAULT)
    out = DingtalkCardRenderer().render(card).payload
    assert out["msgtype"] == "actionCard"
    ac = out["actionCard"]
    assert ac["title"] == "标题"
    assert "正文" in ac["text"]
    assert not ac.get("btns")   # 无动作 → 无空按钮


def test_dingtalk_url_button_and_param_action_degrades():
    card = Card(title="t", blocks=(text_block("body"),), actions=(
        Action(id="open", label="打开", url="https://example.com"),
        Action(id="bind", label="绑定", params={"num": 1}),
    ))
    rendered = DingtalkCardRenderer().render(card)
    ac = rendered.payload["actionCard"]
    assert ac["btns"] == [{"title": "打开", "actionURL": "https://example.com"}]
    assert "回复 `/bind 1`" in ac["text"]          # 降级为命令提示（内容不丢）
    assert rendered.degraded == ["card_action_unsupported"]


def test_dingtalk_select_rendered_as_numbered_list():
    card = _autopilot_list_ir()
    text = DingtalkCardRenderer().render(card).payload["actionCard"]["text"]
    assert "1. finding one" in text


# ---------------------------------------------------------------- 共享 text renderer

def test_option_label_existing_number_not_double_numbered():
    card = Card(title="t", blocks=(
        select_block("选择", [Action(id="s", label="1. finding one"),
                              Action(id="s2", label="finding two")]),))
    dt = DingtalkCardRenderer().render(card).payload["actionCard"]["text"]
    assert "1. 1. finding one" not in dt
    assert "1. finding one" in dt and "2. finding two" in dt
    text = render_text(card)
    assert "1. 1. finding one" not in text
    assert "2. finding two" in text


def test_text_renderer_keeps_all_visible_text_and_action_hints():
    card = _repo_card_ir()
    text = render_text(card)
    assert "**📁 选择仓库**" in text
    assert "列表文本" in text
    assert "1: 回复 `/bind 1`" in text or "1: 回复 `/bind 1`".replace(" ", "") in text.replace(" ", "")
    assert "/help" in text


def test_text_renderer_select_and_hint():
    card = _autopilot_list_ir()
    text = render_text(card)
    assert "1. finding one" in text
    assert "选择要派发的问题" in text


def test_text_renderer_footer():
    card = Card(title="t", blocks=(text_block("b"),), page=(2, 3), hint="回复编号选择 · 0 取消")
    text = render_text(card)
    assert "第 2/3 页" in text
    assert "回复编号选择 · 0 取消" in text


def test_text_renderer_url_action():
    text = render_text(simple_card("t", "b", Tone.DEFAULT, Action(id="open", label="打开", url="https://x")))
    assert "https://x" in text


# ---------------------------------------------------------------- 能力位 + 启动自检

def test_card_action_capability_bit_and_describe():
    assert Capability.CARD_ACTION == 1 << 8
    assert "CARD_ACTION" in describe(Capability.TEXT | Capability.CARD_ACTION)


def test_validate_card_capability_flags_rich_card_without_renderer():
    problem = validate_card_capability(Capability.RICH_CARD, has_renderer=False)
    assert "RICH_CARD" in problem
    assert validate_card_capability(Capability.RICH_CARD, has_renderer=True) == ""
    assert validate_card_capability(Capability.TEXT, has_renderer=False) == ""


def test_self_check_builtin_channels_clean():
    from src.im.adapters.dingtalk.outbound import DINGTALK_CAPABILITIES
    from src.im.adapters.feishu.outbound import FEISHU_CAPABILITIES
    assert self_check_card_capabilities(
        {"feishu": FEISHU_CAPABILITIES, "dingtalk": DINGTALK_CAPABILITIES}) == []
    # 非内置渠道声明 RICH_CARD 但无 renderer → 告警清单非空
    # （Issue #1309：slack 已是内置渲染渠道；#1613：telegram 也已是内置渲染渠道，
    #   改用仍未接入的 whatsapp 作反例）
    assert self_check_card_capabilities({"whatsapp": Capability.RICH_CARD}) != []


def test_feishu_declares_card_action_dingtalk_does_not():
    from src.im.adapters.dingtalk.outbound import DINGTALK_CAPABILITIES
    from src.im.adapters.feishu.outbound import FEISHU_CAPABILITIES
    assert FEISHU_CAPABILITIES & Capability.CARD_ACTION
    assert not (DINGTALK_CAPABILITIES & Capability.CARD_ACTION)


def test_card_ir_switch_default_on_and_env_off(monkeypatch):
    monkeypatch.delenv(CARD_IR_ENV, raising=False)
    assert card_ir_enabled()
    monkeypatch.setenv(CARD_IR_ENV, "off")
    assert not card_ir_enabled()
    monkeypatch.setenv(CARD_IR_ENV, "on")
    assert card_ir_enabled()


# ---------------------------------------------------------------- egress 路由 + 硬护栏

class _ReplierStub:
    def __init__(self, caps):
        self._caps = caps
        self.sent = []

    def caps(self):
        return self._caps

    async def reply(self, out):
        self.sent.append(out)

        class _R:
            ok = True
            message_id = "m1"
            error = ""
        return _R()


class _MetricsStub:
    def __init__(self):
        self.failed = 0
        self.reasons = []

    def record_failed(self):
        self.failed += 1

    def record_degraded(self, reason=""):
        self.reasons.append(reason)


def _ctx():
    return object()


def test_egress_routes_card_ir_to_channel_renderer():
    replier = _ReplierStub(Capability.TEXT | Capability.RICH_CARD)
    m = _MetricsStub()
    eg = HandlerEgress(None, replier, metrics=m, card_renderer=FeishuCardRenderer())
    ok = asyncio.run(eg.send_card(_ctx(), "msg1", _repo_card_ir(), chat_id="oc1"))
    assert ok
    out = replier.sent[0]
    assert out.metadata["kind"] == "raw_card"
    assert out.metadata["raw_card"] == REPO_CARD_SNAPSHOT
    assert not [r for r in m.reasons if r == "card_text_fallback"]


def test_egress_falls_back_to_text_without_renderer_and_counts_degraded():
    replier = _ReplierStub(Capability.TEXT)   # 无 RICH_CARD
    m = _MetricsStub()
    eg = HandlerEgress(None, replier, metrics=m, card_renderer=None)
    ok = asyncio.run(eg.send_card(_ctx(), "msg1", _repo_card_ir(), chat_id="oc1"))
    assert ok
    out = replier.sent[0]
    assert out.metadata["kind"] == "text"
    assert "📁 选择仓库" in out.text
    assert "card_text_fallback" in m.reasons


def test_egress_empty_card_content_hard_guard():
    replier = _ReplierStub(Capability.TEXT | Capability.RICH_CARD)
    m = _MetricsStub()
    eg = HandlerEgress(None, replier, metrics=m, card_renderer=FeishuCardRenderer())
    res = asyncio.run(eg._send(
        type("O", (), {"chat_id": "oc1", "text": "  ", "reply_to": None,
                       "metadata": {"kind": "text"}})()))
    assert not res.ok
    assert "empty content guard" in res.error
    assert m.failed == 1
    assert "card_empty_guard" in m.reasons


def test_egress_dingtalk_renderer_produces_action_card_metadata():
    from src.im.adapters.dingtalk.outbound import DINGTALK_CAPABILITIES
    replier = _ReplierStub(DINGTALK_CAPABILITIES)
    eg = HandlerEgress(None, replier, metrics=_MetricsStub(),
                       card_renderer=DingtalkCardRenderer())
    asyncio.run(eg.send_card(_ctx(), "", simple_card("t", "b"), chat_id="cid1"))
    out = replier.sent[0]
    assert out.metadata["raw_card"]["msgtype"] == "actionCard"


def test_egress_records_renderer_degraded_reasons():
    replier = _ReplierStub(Capability.TEXT | Capability.RICH_CARD)
    m = _MetricsStub()
    eg = HandlerEgress(None, replier, metrics=m, card_renderer=DingtalkCardRenderer())
    card = simple_card("t", "b", Tone.DEFAULT, Action(id="bind", params={"num": 1}))
    asyncio.run(eg.send_card(_ctx(), "", card, chat_id="cid1"))
    assert "card_action_unsupported" in m.reasons


def test_update_card_by_token_preserves_renderer_degraded():
    replier = _ReplierStub(Capability.TEXT | Capability.RICH_CARD)  # 无 MESSAGE_EDIT
    m = _MetricsStub()
    eg = HandlerEgress(None, replier, metrics=m, card_renderer=DingtalkCardRenderer())
    card = simple_card("t", "b", Tone.DEFAULT, Action(id="bind", params={"num": 1}))
    ok = asyncio.run(eg.update_card_by_token(_ctx(), "tok", card, chat_id="cid1"))
    assert ok
    assert "card_action_unsupported" in m.reasons
    assert replier.sent[0].metadata["kind"] == "raw_card"


def test_egress_raw_card_empty_payload_hard_guard():
    replier = _ReplierStub(Capability.TEXT | Capability.RICH_CARD)
    m = _MetricsStub()
    eg = HandlerEgress(None, replier, metrics=m, card_renderer=FeishuCardRenderer())
    empty = {"msgtype": "actionCard", "actionCard": {"title": "t", "text": "  "}}
    res = asyncio.run(eg._send(
        type("O", (), {"chat_id": "cid1", "text": "", "reply_to": None,
                       "metadata": {"kind": "raw_card", "raw_card": empty}})()))
    assert not res.ok
    assert "empty content guard" in res.error
    assert m.failed == 1
    assert "card_empty_guard" in m.reasons


def test_egress_legacy_mode_renders_lark_dict_passthrough():
    class _LegacyApi:
        async def update_card_by_token(self, ctx, token, card):
            self.card = card
            return True

    api = _LegacyApi()
    eg = HandlerEgress(api, None, mode="legacy", card_renderer=FeishuCardRenderer())
    asyncio.run(eg.update_card_by_token(_ctx(), "tok", _autopilot_list_ir()))
    assert api.card["schema"] == "2.0"


# ---------------------------------------------------------------- DingtalkSender 空消息护栏

def _dt_sender(monkeypatch=None):
    from src.im.adapters.dingtalk.outbound import DingtalkSender
    sent = {}

    async def _text(cid, text, **kw):
        sent["text"] = text
        return "mid"

    async def _md(cid, text, **kw):
        sent["md"] = text
        return "mid"

    async def _ac(cid, title, text, btns=None, **kw):
        sent["ac"] = (title, text, btns)
        return "mid"

    return DingtalkSender(_text, _md, _ac), sent


def _dt_out(text="", md=None, **kw):
    from src.im.channel.message import OutboundMessage
    return OutboundMessage(chat_id="cid1", text=text, metadata=md or kw)


def test_dingtalk_sender_empty_text_is_failed_not_silent():
    sender, sent = _dt_sender()
    res = asyncio.run(sender.send(_dt_out("")))
    assert not res.ok
    assert "empty content guard" in res.error
    assert "text" not in sent   # 未发出空消息


def test_dingtalk_sender_sends_action_card_payload():
    sender, sent = _dt_sender()
    payload = DingtalkCardRenderer().render(simple_card("标题", "正文")).payload
    res = asyncio.run(sender.send(_dt_out(md={"kind": "raw_card", "raw_card": payload})))
    assert res.ok
    assert sent["ac"][0] == "标题"
    assert "正文" in sent["ac"][1]


def test_dingtalk_sender_refuses_lark_raw_card():
    sender, _ = _dt_sender()
    lark = {"header": {}, "elements": []}
    res = asyncio.run(sender.send(_dt_out(md={"kind": "raw_card", "raw_card": lark})))
    assert not res.ok
    assert "refused" in res.error


def test_dingtalk_empty_action_card_transport_guard():
    from src.im.adapters.dingtalk.outbound import (
        DingtalkWebhookError,
        WebhookRegistry,
        send_action_card_via_webhook,
    )
    reg = WebhookRegistry()
    reg.register("cid9", "https://webhook")
    with pytest.raises(DingtalkWebhookError):
        asyncio.run(send_action_card_via_webhook("cid9", "t", "   ", btns=[], registry=reg))


def test_dingtalk_action_card_falls_back_to_robot_when_webhook_missing():
    from src.im.adapters.dingtalk.outbound import (
        WebhookRegistry,
        send_action_card_with_fallback,
    )

    class _Robot:
        def __init__(self):
            self.calls = []

        async def send_p2p(self, staff_id, text, *, markdown=True, title=""):
            self.calls.append((staff_id, text, markdown, title))
            return "mid"

    reg = WebhookRegistry()
    reg.register("cid1", "", staff_id="u1", chat_type="1")
    robot = _Robot()
    res = asyncio.run(send_action_card_with_fallback(
        "cid1", "标题", "正文",
        btns=[{"title": "打开", "actionURL": "https://example.com"}],
        robot=robot, registry=reg))
    assert res == "mid"
    staff_id, text, markdown, title = robot.calls[0]
    assert staff_id == "u1" and markdown and title == "标题"
    assert "正文" in text and "打开: https://example.com" in text


# ---------------------------------------------------------------- legacy dict 降级补齐按钮/下拉

def test_flatten_card_keeps_buttons_and_select():
    """附带缺陷修复：_flatten_card 不再丢按钮/下拉（保留动作命令提示）。"""
    from src.im.engine.egress import _flatten_card
    card = {
        "header": {"title": {"content": "标题"}},
        "elements": [
            {"tag": "div", "text": {"content": "正文"}},
            {"tag": "action", "actions": [
                {"text": {"content": "帮助"}, "value": {"cmd": "/help"}}]},
            {"tag": "select_static", "options": [
                {"text": {"content": "选项一"}, "value": "{}"}]},
        ],
    }
    text = _flatten_card(card)
    assert "正文" in text
    assert "帮助: 回复 `/help`" in text
    assert "1. 选项一" in text


# ---------------------------------------------------------------- metrics degraded_by_reason

def test_metrics_degraded_by_reason_keys_and_bump():
    m = EngineMetrics()
    m.record_degraded("card_text_fallback")
    m.record_degraded("card_action_unsupported")
    m.record_degraded("card_empty_guard")
    m.record_degraded("anything_new")
    snap = m.snapshot()
    assert snap["degraded_by_reason"]["card_text_fallback"] == 1
    assert snap["degraded_by_reason"]["card_action_unsupported"] == 1
    assert snap["degraded_by_reason"]["card_empty_guard"] == 1
    assert snap["degraded_by_reason"]["anything_new"] == 1


def test_metrics_degraded_reason_constants():
    assert set(DEGRADED_REASONS) >= {"card_text_fallback", "card_action_unsupported",
                                     "card_empty_guard"}


def test_metrics_no_arg_record_degraded_still_counts():
    m = EngineMetrics()
    m.record_degraded()
    assert m.snapshot()["total_messages_degraded"] == 1


# ---------------------------------------------------------------- 链路 mock 驱动（/repo /chat /task /help + autopilot）

def _run_command(text):
    from src.im.feishu_async import BotContext
    from src.im.handler_async import process_command_async
    from src.session_manager import db as legacy_db
    ctx = BotContext(app_id="cli_card_ir", app_secret="s", bot_type="chat_bot", name="t")
    api = AsyncMock()
    asyncio.run(process_command_async(ctx, api, text, "oc_ir", "sender_1", legacy_db,
                                      account=ctx.app_id, msg_id="msg_ir",
                                      sender_open_id="ou_ir"))
    return api


def _last_card(api):
    src = api.send_raw_card
    assert src.called, "应经 egress.send_raw_card 发送 Card IR"
    return src.call_args[0][2]


def _clean_pending():
    from src.im.handler_async import _clear_pending_selection, _repo_bind_cache
    _clear_pending_selection("oc_ir")
    _repo_bind_cache.clear()


def test_repo_chain_emits_card_ir_with_bind_actions():
    from unittest import mock
    _clean_pending()
    with mock.patch("src.im.handler_async.get_user_id_by_app_id", return_value=1), \
         mock.patch("src.user.db.get_managed_repos", return_value=[
             {"repo_key": "gitcode:owner/a", "is_admin": 1, "is_join": 0},
             {"repo_key": "gitcode:owner/b", "is_admin": 0, "is_join": 1},
         ]), \
         mock.patch("src.im.handler_async._current_bound_repo", return_value=""):
        api = _run_command("/repo")
    card = _last_card(api)
    assert isinstance(card, Card)
    assert card.title == "📁 选择仓库"
    bind_nums = [a.params.get("num") for b in card.blocks for a in (b.options or ())
                 if a.id == "bind"]
    assert bind_nums == [1, 2]
    text = FeishuCardRenderer().render(card).payload
    assert json.dumps(text, ensure_ascii=False).count('"/bind') == 2


def test_help_chain_routes_quick_cmd_card():
    _clean_pending()
    with __import__("unittest").mock.patch(
            "src.im.handler_async.get_user_id_by_app_id", return_value=1):
        api = _run_command("/help")
    assert api.send_quick_cmd_card.called


def test_task_chain_without_repo_sends_no_repo_card_ir():
    from src.im.handler_async import _clear_pending_selection, _repo_bind_cache
    _clear_pending_selection("oc_ir")
    _repo_bind_cache.clear()
    with __import__("unittest").mock.patch(
            "src.im.handler_async.get_user_id_by_app_id", return_value=1), \
         __import__("unittest").mock.patch(
            "src.im.handler_async._current_bound_repo", return_value=""):
        api = _run_command("/task")
    card = _last_card(api)
    assert isinstance(card, Card)
    assert card.title == "⚠️ 未绑定仓库"
    assert card.tone == Tone.DANGER
    ids = [a.id for a in card.actions]
    assert ids == ["repo", "help"]


def test_chat_chain_without_repo_sends_no_repo_card_ir():
    from src.im.handler_async import _clear_pending_selection, _repo_bind_cache
    _clear_pending_selection("oc_ir")
    _repo_bind_cache.clear()
    with __import__("unittest").mock.patch(
            "src.im.handler_async.get_user_id_by_app_id", return_value=1), \
         __import__("unittest").mock.patch(
            "src.im.handler_async._current_bound_repo", return_value=""):
        api = _run_command("/chat")
    card = _last_card(api)
    assert isinstance(card, Card)
    assert [a.id for a in card.actions] == ["repo", "help"]


def test_autopilot_state_card_ir_roundtrip_to_legacy_json():
    """autopilot 三态 IR → 飞书渲染 → 回调载荷可被既有 cmd 路由消费。"""
    from src.im.handler_async import _autopilot_state_card
    info = {"task_name": "demo", "summary": "结论", "scenes": [
        {"scene_id": "s1", "name": "场景一"}],
        "findings": {"findings": [
            {"id": "f1", "title": "finding one", "severity": "high",
             "description": "d", "dispatched_issue_number": None}]}}
    for state, kw in (("list", {}), ("scene", {"finding_id": "f1"}),
                      ("result", {"finding_id": "f1"})):
        card = _autopilot_state_card(info, 7, state=state, **kw)
        assert isinstance(card, Card) and card.form == "v2"
        payload = FeishuCardRenderer().render(card).payload
        assert payload["schema"] == "2.0"
        text = json.dumps(payload, ensure_ascii=False)
        for banned in ("behaviors_value_leak",):
            assert banned not in text
        # 语义动作可映射回命令（回收侧零改动）
        assert "/finding-" in text


def test_bind_success_card_ir_and_renderer():
    from src.im.handler_async import _bind_success_card
    card = _bind_success_card("gitcode:owner/a")
    assert card.title == "✅ 绑定成功"
    out = FeishuCardRenderer().render(card).payload
    assert out["header"]["template"] == "green"
    assert out["elements"][0]["text"]["content"].startswith("**✅ 已绑定 `gitcode:owner/a`**")


# ---------------------------------------------------------------- 静态检查：业务层禁平台字段

_PLATFORM_FIELD_RE = re.compile(r"lark_md|behaviors|\"tag\"\s*:|'tag'\s*:|<font|<at")
_BUSINESS_FILES = ["src/im/handler_async.py", "src/im/vod_handler_async.py"]


@pytest.mark.parametrize("rel", _BUSINESS_FILES)
def test_business_layer_has_no_platform_card_fields(rel):
    """验收 6：业务层禁止出现 lark_md / behaviors / tag 等平台字段。"""
    source = (Path(ROOT) / rel).read_text(encoding="utf-8")
    matches = _PLATFORM_FIELD_RE.findall(source)
    assert not matches, f"{rel} 泄漏平台卡片字段：{sorted(set(matches))}"
