"""Slack 卡片测试（Issue #1309 §8.4 / §8.5）——Block Kit 映射与上限降级。

覆盖：
  - Card IR → Block Kit（标题/文本/字段/分隔线/按钮/页脚）；
  - `section.text` 3000 字符切分（超长文本不丢内容）；
  - **>25 按钮降级**为「回复 `/cmd`」文本并计 `card_action_unsupported`；
  - 50 blocks 上限守卫；
  - 动作往返：renderer 写出的 value → `block_actions` → `/bind 2`（同一份 `_handle_selection`）；
  - egress 走 slack renderer（payload=blocks，degraded 归因透传）。
"""
from src.im.adapters.slack.cards import (
    MAX_ACTION_ELEMENTS,
    MAX_BLOCKS,
    MAX_FIELD_CHARS,
    MAX_SECTION_CHARS,
    parse_action_value,
)
from src.im.adapters.slack.inbound import block_actions_to_inbound
from src.im.channel.card import (
    Action,
    ButtonStyle,
    Card,
    Tone,
    action_payload,
    divider_block,
    fields_block,
    row_block,
    select_block,
    text_block,
)
from src.im.channel.renderers.slack import SlackCardRenderer

RENDERER = SlackCardRenderer()


def _button_actions(value=None):
    blocks = value["blocks"] if isinstance(value, dict) and "blocks" in value else value
    out = []
    for block in blocks:
        if block.get("type") == "actions":
            out.extend(block.get("elements") or [])
    return out


# ── 基础映射 ───────────────────────────────────────────────

def test_render_basic_blocks():
    card = Card(
        title="选择仓库",
        tone=Tone.INFO,
        blocks=(
            text_block("**请选择** 要绑定的仓库"),
            fields_block(("数量", "2"), ("范围", "user")),
            divider_block(),
            row_block(Action(id="bind", label="绑定 #1", params={"num": 1}),
                      Action(id="cancel", label="取消")),
        ),
    )
    rendered = RENDERER.render(card)
    blocks = rendered.payload["blocks"]
    assert blocks[0]["type"] == "header" and blocks[0]["text"]["text"] == "选择仓库"
    assert blocks[1]["type"] == "section"
    assert blocks[1]["text"]["text"] == "*请选择* 要绑定的仓库"
    assert blocks[2]["type"] == "section" and len(blocks[2]["fields"]) == 2
    assert any(b["type"] == "divider" for b in blocks)
    buttons = _button_actions(blocks)
    assert [b["action_id"] for b in buttons] == ["bind", "cancel"]
    assert parse_action_value(buttons[0]["value"]) == {"cmd": "/bind 1"}
    assert rendered.degraded == []


def test_render_splits_long_section():
    text = "长" * (MAX_SECTION_CHARS * 2 + 10)
    rendered = RENDERER.render(Card(blocks=(text_block(text),)))
    sections = [b for b in rendered.payload["blocks"] if b["type"] == "section"]
    assert len(sections) >= 3
    assert all(len(b["text"]["text"]) <= MAX_SECTION_CHARS for b in sections)
    assert "".join(b["text"]["text"] for b in sections) == text


def test_render_splits_long_field_under_2000():
    """section.fields 每字段硬上限 2000（超长拆字段，不发出被拒 payload）。"""
    value = "值" * (MAX_FIELD_CHARS * 2 + 10)
    rendered = RENDERER.render(Card(blocks=(fields_block(("说明", value)),)))
    fields = [f for b in rendered.payload["blocks"] if b["type"] == "section"
              for f in (b.get("fields") or [])]
    assert fields
    assert all(len(f["text"]) <= MAX_FIELD_CHARS for f in fields)
    assert "".join(f["text"] for f in fields) == f"*说明*: {value}"


def test_render_url_action_is_link_button():
    rendered = RENDERER.render(Card(actions=(Action(id="open", label="打开", url="https://x/y"),)))
    button = _button_actions(rendered.payload["blocks"])[0]
    assert button["url"] == "https://x/y"
    assert "value" not in button
    assert button["text"]["text"] == "打开"


def test_render_primary_style_mapped():
    rendered = RENDERER.render(Card(actions=(Action(
        id="go", label="继续", style=ButtonStyle.PRIMARY),)))
    assert _button_actions(rendered.payload["blocks"])[0]["style"] == "primary"


def test_render_select_options_become_buttons():
    rendered = RENDERER.render(Card(blocks=(select_block(
        "选择一项",
        [Action(id="bind", label=f"仓库{i}", params={"num": i}) for i in range(1, 4)],
    ),)))
    blocks = rendered.payload["blocks"]
    assert blocks[0]["type"] == "section"
    assert [b["action_id"] for b in _button_actions(blocks)] == ["bind"] * 3


def test_render_footer_context():
    rendered = RENDERER.render(Card(hint="回复编号选择 · 0 取消", page=(2, 5)))
    footer = [b for b in rendered.payload["blocks"] if b["type"] == "context"]
    assert any("第 2/5 页" in b["elements"][0]["text"] for b in footer)
    assert any("回复编号选择" in b["elements"][0]["text"] for b in footer)


# ── 上限与降级 ─────────────────────────────────────────────

def test_over_25_buttons_degrade_to_text_hint():
    actions = [Action(id="bind", label=f"仓库{i}", params={"num": i}) for i in range(1, 31)]
    rendered = RENDERER.render(Card(blocks=(row_block(*actions),)))
    buttons = _button_actions(rendered.payload["blocks"])
    assert len(buttons) <= MAX_ACTION_ELEMENTS
    assert "card_action_unsupported" in rendered.degraded
    hints = [b for b in rendered.payload["blocks"]
             if b["type"] == "section" and "回复 `" in (b.get("text") or {}).get("text", "")]
    assert hints, rendered.payload["blocks"]
    hint_text = hints[0]["text"]["text"]
    assert "/bind 26" in hint_text           # 第 26 个起降级为命令提示
    assert "仓库26" in hint_text             # 动作语义不丢


def test_max_blocks_guard_degrades_without_losing_content():
    blocks = tuple(text_block(f"第{i}段") for i in range(MAX_BLOCKS + 5))
    rendered = RENDERER.render(Card(blocks=blocks))
    assert len(rendered.payload["blocks"]) <= MAX_BLOCKS
    assert "card_text_fallback" in rendered.degraded
    text = "".join(
        str((b.get("text") or {}).get("text") or "")
        for b in rendered.payload["blocks"])
    for i in range(MAX_BLOCKS + 5):
        assert f"第{i}段" in text


def test_empty_card_degrades_to_fallback_reason():
    rendered = RENDERER.render(Card())
    assert rendered.payload["blocks"] == []
    assert "card_text_fallback" in rendered.degraded


# ── 回调往返 ───────────────────────────────────────────────

def test_button_value_roundtrips_to_command():
    card = Card(blocks=(row_block(
        Action(id="bind", label="仓库 2", params={"num": 2}),
        Action(id="join", label="加入", params={"num": 3}),
    ),))
    buttons = _button_actions(RENDERER.render(card).payload["blocks"])
    bind_value = buttons[0]["value"]
    assert bind_value == '{"cmd": "/bind 2"}'
    payload = {
        "type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"},
        "message": {"ts": "1.1"},
        "actions": [{"action_id": "bind", "value": bind_value}],
    }
    msg = block_actions_to_inbound(payload)
    assert msg is not None and msg.text == "/bind 2"
    assert parse_action_value(msg.raw["action_value"])["cmd"] == "/bind 2"


def test_action_payload_matches_renderer_value():
    action = Action(id="bind", label="x", params={"num": 4})
    assert parse_action_value(
        _button_actions(RENDERER.render(Card(actions=(action,))).payload["blocks"])[0]["value"]
    ) == action_payload(action)


# ── egress 集成（renderer 注入 + degraded 归因）─────────────

def test_egress_renders_card_to_blocks_and_keeps_degraded():
    from src.im.engine.egress import HandlerEgress
    from src.im.channel.capability import Capability

    class _Replier:
        def caps(self):
            return Capability.TEXT | Capability.RICH_CARD

    egress = HandlerEgress(None, _Replier(), mode="replier", card_renderer=RENDERER)
    actions = [Action(id="bind", label=f"仓库{i}", params={"num": i}) for i in range(1, 31)]
    payload, degraded = egress._render_card(Card(title="t", blocks=(row_block(*actions),)), RENDERER)
    assert isinstance(payload, dict) and "blocks" in payload
    assert "card_action_unsupported" in degraded


def test_slack_capability_self_check_is_clean():
    from src.im.adapters.slack.outbound import SLACK_CAPABILITIES
    from src.im.channel.card import self_check_card_capabilities

    assert self_check_card_capabilities({"slack": SLACK_CAPABILITIES}) == []


# ── 回归：Slack blocks 载荷不得被空内容护栏误判 ────────────────
# 线上故障（2026-11-09）：/repo 等所有卡片恒报「当前渠道不支持卡片消息」。
# 根因：_card_payload_empty 不认识 {"blocks":[...]}，fallback 的 _flatten_card
# 只抽 lark header/elements → 恒返回空串 → 有内容的卡片被判空拦截。

def test_slack_blocks_payload_is_not_treated_as_empty():
    from src.im.engine.egress import _card_payload_empty

    rendered = RENDERER.render(Card(
        title="📁 选择仓库",
        blocks=(
            text_block("已托管仓库列表（共 2 个）"),
            row_block(Action(id="bind", label="1", params={"num": 1})),
        ),
    ))
    assert rendered.payload.get("blocks"), "前提：renderer 必须产出非空 blocks"
    assert _card_payload_empty(rendered.payload) is False


def test_empty_blocks_payload_still_guarded():
    from src.im.engine.egress import _card_payload_empty

    assert _card_payload_empty({"blocks": []}) is True
    assert _card_payload_empty({"blocks": [{"type": "divider"}]}) is False
