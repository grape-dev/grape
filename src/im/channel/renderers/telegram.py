"""Telegram CardRenderer：Card IR → 文本(HTML) + InlineKeyboardMarkup（Issue #1613 §3.4）。

渲染契约（`channel/renderers/__init__.py` RenderedCard）：
  payload = `{"text": ..., "reply_markup": {"inline_keyboard": [...]}, "parse_mode": "HTML"}`，
  degraded 收集降级原因（由 egress 计 metrics）。

映射（业务层零改动）：
  - `title`   → 文本首行加粗 `<b>…</b>`；
  - `text`    → 正文段落（HTML parse_mode）；
  - `fields`  → `键：值` 行；
  - `divider` → `──────────` 分隔行；
  - `select` / `row_block` / `button` / `card.actions` → inline_keyboard 行；
  - `page`    → 末行 `第 x/y 页`；`hint` → 末行提示。

上限（Telegram 硬约束）：
  - `callback_data` 1–64 字节 → 短码；超长走 `TelegramCardLedger` 的 `c:<cid>`；
    仍放不下则按钮降级为「回复 `/cmd`」文本（计 `card_action_unsupported`）；
  - 按钮总数 ≤25、每行 ≤8（与 Slack 对齐）；超出转文本。
"""
from __future__ import annotations

from ..card import Action, Card, render_text
from . import CardRenderer, RenderedCard
from ...adapters.telegram.cards import (
    MAX_BUTTONS,
    TelegramCardLedger,
    default_ledger,
    action_hint,
    inline_keyboard,
)
from ...adapters.telegram.markdown import format_html

_DIVIDER = "──────────"


class TelegramCardRenderer(CardRenderer):
    """Card IR → Telegram 文本 + InlineKeyboardMarkup 载荷。"""

    def __init__(self, ledger: TelegramCardLedger | None = None) -> None:
        self._ledger = ledger if ledger is not None else default_ledger()

    @property
    def ledger(self) -> TelegramCardLedger:
        return self._ledger

    def render(self, card: Card) -> RenderedCard:
        degraded: list[str] = []
        lines: list[str] = []
        pending: list[Action] = []
        hints: list[str] = []

        if card.title:
            lines.append("<b>" + format_html(card.title) + "</b>")

        for block in card.blocks:
            if block.kind == "text":
                if block.text:
                    lines.append(format_html(block.text))
            elif block.kind == "fields":
                for key, value in block.fields:
                    lines.append(f"{format_html(str(key))}：{format_html(str(value))}")
            elif block.kind == "divider":
                lines.append(_DIVIDER)
            elif block.kind == "select":
                if block.placeholder:
                    lines.append(format_html(block.placeholder))
                pending.extend(block.options)
            elif block.kind == "actions":
                pending.extend(block.options)
            elif block.kind == "button" and block.action is not None:
                pending.append(block.action)

        pending.extend(card.actions)
        rows, overflow_hints = inline_keyboard(pending, self._ledger)
        if overflow_hints:
            hints.extend(overflow_hints)
            degraded.append("card_action_unsupported")

        if hints:
            lines.append("\n".join(hints))
        if card.page:
            lines.append(f"第 {card.page[0]}/{card.page[1]} 页")
        if card.hint:
            lines.append(format_html(card.hint))

        text = "\n".join(line for line in lines if line).strip()
        if not text:
            # 空卡片：降级为纯文本（egress 会按空载荷护栏计 failed，不静默发空消息）
            fallback = render_text(card)
            if fallback.strip():
                text = format_html(fallback)
            else:
                degraded.append("card_text_fallback")

        payload: dict = {"text": text, "parse_mode": "HTML"}
        if rows:
            payload["reply_markup"] = {"inline_keyboard": rows}

        return RenderedCard(payload=payload, degraded=degraded)


__all__ = ["MAX_BUTTONS", "TelegramCardRenderer"]