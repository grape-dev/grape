"""企微 Card IR 渲染测试（Issue #1254 批 3）。

覆盖：
  - Card IR → template_card 载荷（text_notice / button_interaction）；
  - select 选项 → 可点按钮，key 解回 `/bind 2`（与「回复数字」同一处理路径）；
  - 无模板 → markdown 降级 + `card_action_unsupported`（内容不丢、不静默）；
  - payload 通过 egress 空载荷护栏（否则卡片永远发不出去）；
  - 业务层零平台字段（`tests/im/test_card_ir.py` 静态检查的配套断言）；
  - 台账 register/lookup/TTL。
"""
import json

import pytest

from src.im.adapters.wecom.cards import (
    CardLedger,
    build_template_card,
    decode_action_key,
    encode_action_key,
    event_key_to_command,
)
from src.im.channel.card import (
    Action,
    Card,
    button_block,
    divider_block,
    fields_block,
    row_block,
    select_block,
    simple_card,
    text_block,
)
from src.im.channel.renderers.wecom import WecomCardRenderer
from src.im.engine.egress import _card_payload_empty


def _bind_options(n=3):
    return [Action(id="bind", label=f"repo-{i}", params={"num": str(i)}) for i in range(1, n + 1)]


def test_simple_card_renders_text_notice_with_title_and_body():
    card = simple_card("绑定结果", "✅ 已绑定 repo-a")
    payload = build_template_card(card)
    assert payload["msgtype"] == "template_card"
    template = payload["template_card"]
    assert template["card_type"] == "text_notice"
    assert template["main_title"]["title"] == "绑定结果"
    assert "已绑定 repo-a" in template["sub_title_text"]
    assert "button_list" not in template


def test_select_options_become_clickable_buttons_with_decodable_keys():
    card = Card(title="选择仓库", blocks=(text_block("请选择要绑定的仓库"),
                                      select_block("可用仓库", _bind_options())))
    payload = build_template_card(card)
    template = payload["template_card"]
    assert template["card_type"] == "button_interaction"
    assert template["task_id"]                     # 真回调需要 task_id
    keys = [b["key"] for b in template["button_list"]]
    assert len(keys) == 3
    assert event_key_to_command(keys[1]) == "/bind 2"   # FR-6：点「2」= 回复 2
    assert decode_action_key(keys[0])["cmd"] == "/bind 1"


def test_fields_and_link_actions_render_to_platform_structures():
    card = Card(title="任务", blocks=(fields_block(("状态", "运行中"), ("耗时", "3s")),
                                     divider_block()),
                actions=(Action(id="open", label="查看详情", url="https://example.com/t/1"),))
    template = build_template_card(card)["template_card"]
    assert {"keyname": "状态", "value": "运行中"} in template["horizontal_content_list"]
    assert template["jump_list"][0]["url"] == "https://example.com/t/1"
    assert "button_list" not in template            # 只有跳转动作 → 不出回调按钮


def test_button_overflow_degrades_to_command_hint():
    card = Card(title="很多选项", blocks=(select_block("仓库", _bind_options(10)),))
    template = build_template_card(card)["template_card"]
    assert len(template["button_list"]) == 6       # 上限内给按钮
    assert "/bind 10" in template["sub_title_text"]  # 超出的用文字提示，动作不丢


def test_renderer_payload_passes_egress_empty_guard():
    """egress 护栏按 msgtype=markdown 判空：载荷必须自带 markdown 兜底文本。"""
    renderer = WecomCardRenderer(template_enabled=True)
    rendered = renderer.render(simple_card("标题", "正文"))
    payload = rendered.payload
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["text"].strip()
    assert _card_payload_empty(payload) is False
    assert payload["template_card"]["main_title"]["title"] == "标题"


def test_renderer_without_template_degrades_to_markdown():
    renderer = WecomCardRenderer(template_enabled=False)
    card = Card(title="选择仓库", blocks=(select_block("仓库", _bind_options()),))
    rendered = renderer.render(card)
    assert rendered.degraded == ["card_action_unsupported"]
    assert "template_card" not in rendered.payload
    assert "repo-1" in rendered.payload["markdown"]["text"]


def test_renderer_env_switch(monkeypatch):
    monkeypatch.setenv("GRAPE_WECOM_TEMPLATE_CARD", "off")
    rendered = WecomCardRenderer().render(simple_card("标题", "正文"))
    assert "template_card" not in rendered.payload
    assert rendered.degraded == ["card_action_unsupported"]


def test_empty_card_payload_still_trips_guard():
    renderer = WecomCardRenderer(template_enabled=False)
    rendered = renderer.render(Card())
    assert _card_payload_empty(rendered.payload) is True


def test_renderer_does_not_leak_lark_shapes():
    payload = WecomCardRenderer().render(
        Card(title="t", blocks=(row_block(Action(id="help", label="帮助")),))).payload
    blob = json.dumps(payload, ensure_ascii=False)
    assert "lark_md" not in blob and "behaviors" not in blob and "elements" not in blob


def test_encode_decode_action_key_roundtrip():
    action = Action(id="join", label="加入", params={"num": 7})
    key = encode_action_key(action)
    assert decode_action_key(key) == {"cmd": "/join 7"}
    assert decode_action_key("{bad json") is None


def test_event_key_non_json_falls_back():
    assert event_key_to_command("approve") == "/approve"
    assert event_key_to_command("/bind 3") == "/bind 3"
    assert event_key_to_command("") == ""


def test_ledger_register_lookup_and_ttl():
    now = [100.0]
    ledger = CardLedger(ttl=10.0, clock=lambda: now[0])
    ledger.register("task_1", "chat_9", 2)
    record = ledger.lookup("task_1")
    assert record and record.chat_id == "chat_9" and record.chat_type == 2
    now[0] += 20.0
    assert ledger.lookup("task_1") is None


def test_ledger_is_bounded():
    now = [0.0]
    ledger = CardLedger(ttl=10_000.0, max_entries=3, clock=lambda: now[0])
    for i in range(6):
        now[0] += 1.0
        ledger.register(f"t{i}", f"c{i}")
    assert len(ledger) <= 3
    assert ledger.lookup("t5") is not None