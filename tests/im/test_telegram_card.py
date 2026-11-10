"""Telegram 卡片测试（Issue #1613 批 3/4）——文本 + InlineKeyboardMarkup 与 64B 短码。

覆盖：
  - Card IR → `{text, reply_markup}`（标题/文本/字段/分隔线/按钮/页脚）；
  - callback_data ≤64 字节；短码往返（`b:2` → `/bind 2`）；
  - 超长参数走 `c:<cid>` 台账回查；
  - >25 按钮降级为「回复 `/cmd`」文本并计 `card_action_unsupported`；
  - egress 空载荷护栏认识 `{text, reply_markup}` 形态。
"""
from src.im.adapters.telegram.cards import (
    MAX_BUTTONS,
    MAX_CALLBACK_BYTES,
    TelegramCardLedger,
    decode_callback_data,
    encode_callback_data,
)
from src.im.channel.card import (
    Action,
    Card,
    Tone,
    divider_block,
    fields_block,
    row_block,
    text_block,
)
from src.im.channel.renderers.telegram import TelegramCardRenderer
from src.im.engine.egress import _card_payload_empty

RENDERER = TelegramCardRenderer()


def _buttons(payload):
    keyboard = payload.get("reply_markup") or {}
    return [b for row in keyboard.get("inline_keyboard") or [] for b in row]


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
    payload = rendered.payload
    assert payload["parse_mode"] == "HTML"
    assert "<b>选择仓库</b>" in payload["text"]
    assert "<b>请选择</b>" in payload["text"]
    assert "数量：2" in payload["text"]
    assert "──────────" in payload["text"]
    buttons = _buttons(payload)
    assert [b["text"] for b in buttons] == ["绑定 #1", "取消"]
    assert buttons[0]["callback_data"] == "b:1"
    assert buttons[1]["callback_data"] == "c"
    assert rendered.degraded == []


def test_callback_data_under_64_bytes_and_roundtrip():
    action = Action(id="bind", label="绑定 #2", params={"num": 2})
    data = encode_callback_data(action)
    assert data == "b:2"
    assert len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES
    assert decode_callback_data(data) == {"cmd": "/bind 2"}


def test_long_params_use_ledger_cid():
    ledger = TelegramCardLedger()
    action = Action(id="finding-dispatch", label="确认派发",
                    params={"exec_id": 12, "finding_id": "34",
                            "scene_id": "x" * 80})
    data = encode_callback_data(action, ledger)
    assert data.startswith("c:")
    assert len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES
    assert decode_callback_data(data, ledger) == {
        "cmd": "/finding-dispatch", "exec_id": 12, "finding_id": "34",
        "scene_id": "x" * 80}


def test_short_params_do_not_use_ledger():
    ledger = TelegramCardLedger()
    action = Action(id="finding-dispatch", label="确认派发",
                    params={"exec_id": 12, "finding_id": "34", "scene_id": ""})
    data = encode_callback_data(action, ledger)
    assert data == "fd:12:34:"
    assert len(ledger) == 0


def test_url_action_is_link_button_not_callback():
    rendered = RENDERER.render(Card(actions=(Action(
        id="open", label="打开", url="https://example.com/x"),)))
    button = _buttons(rendered.payload)[0]
    assert button["url"] == "https://example.com/x"
    assert "callback_data" not in button


def test_more_than_25_buttons_degrade_to_text_hint():
    actions = tuple(Action(id="bind", label=f"绑定 {i}", params={"num": i})
                    for i in range(1, MAX_BUTTONS + 5))
    rendered = RENDERER.render(Card(actions=actions))
    buttons = _buttons(rendered.payload)
    assert len(buttons) == MAX_BUTTONS
    assert "card_action_unsupported" in rendered.degraded
    assert "回复 `/bind 26`" in rendered.payload["text"]


def test_escape_html_in_user_content():
    rendered = RENDERER.render(Card(blocks=(text_block("<script> & x"),)))
    assert "&lt;script&gt;" in rendered.payload["text"]
    assert "&amp;" in rendered.payload["text"]


def test_empty_guard_accepts_text_reply_markup_payload():
    payload = {"text": "hi", "parse_mode": "HTML",
               "reply_markup": {"inline_keyboard": [[{"text": "x", "callback_data": "h"}]]}}
    assert _card_payload_empty(payload) is False
    assert _card_payload_empty({"text": "", "reply_markup": {"inline_keyboard": []}}) is True


def test_empty_guard_accepts_text_only_card_without_buttons():
    # 无按钮的纯文本卡片：渲染结果只有 {text, parse_mode}，不含 reply_markup，
    # 旧实现只在 text+reply_markup 同时存在时识别 Telegram → 会误判为空并拦截。
    rendered = RENDERER.render(Card(title="通知", blocks=(text_block("任务已完成"),)))
    payload = rendered.payload
    assert "reply_markup" not in payload
    assert payload["parse_mode"] == "HTML"
    assert _card_payload_empty(payload) is False
    # 正文为空时仍算空载荷
    assert _card_payload_empty({"text": "  ", "parse_mode": "HTML"}) is True

def test_builtin_wiring_and_capability_self_check():
    from src.im.adapters.telegram.outbound import TELEGRAM_CAPABILITIES
    from src.im.channel.card import self_check_card_capabilities
    from src.im.engine_wiring import _card_renderer_for

    assert "telegram" in __import__("src.im.channel.card", fromlist=[
        "BUILTIN_CARD_RENDERERS"]).BUILTIN_CARD_RENDERERS
    assert isinstance(_card_renderer_for("telegram"), TelegramCardRenderer)
    assert self_check_card_capabilities({"telegram": TELEGRAM_CAPABILITIES}) == []
