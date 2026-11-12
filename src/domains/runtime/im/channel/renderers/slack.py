"""Slack CardRenderer：Card IR → Block Kit（Issue #1309 §4.4）。

渲染契约（`channel/renderers/__init__.py` RenderedCard）：
  payload = `{"blocks": [...]}`，degraded 收集降级原因（由 egress 计 metrics）。

映射（业务层零改动）：
  - `title`    → `header` block；
  - `text`     → `section`（>3000 字符按段切分）；
  - `fields`   → `section.fields`（每段 ≤10 个字段）；
  - `divider`  → `divider`；
  - `select` / `row_block` / `button` / `card.actions` → `actions.elements[].button`
    （`action_id=action.id`，`value=json(action_payload(action))`）。

上限（Slack 硬约束）：
  - 单条 ≤50 blocks；超出部分汇总为一条文本 section（计 `card_text_fallback`），
    内容不静默丢弃；
  - 按钮总计 ≤25 个；超出按钮降级为「回复 `/cmd`」文本行（计 `card_action_unsupported`），
    绝不发出会被 Slack 拒收的 payload，也不静默丢动作。
"""
from __future__ import annotations

from ..card import Action, Card, render_text
from . import CardRenderer, RenderedCard
from ...adapters.slack.cards import (
    MAX_ACTION_ELEMENTS,
    MAX_BLOCKS,
    MAX_SECTION_CHARS,
    action_hint,
    actions_block,
    button_element,
    context_block,
    divider_block,
    fields_section,
    header_block,
    section_block,
    section_blocks,
)
from ...adapters.slack.markdown import format_mrkdwn

_BUTTONS_PER_ROW = 5
_OVERFLOW_TRUNCATED_HINT = "\n…（内容过长，其余已截断）"


def _block_text(block: dict) -> str:
    """提取 block 的可读文本（用于 50 blocks 溢出时的内容汇总）。"""
    kind = block.get("type")
    if kind == "header":
        return str((block.get("text") or {}).get("text") or "")
    if kind == "section":
        text = (block.get("text") or {}).get("text")
        if text:
            return str(text)
        return " ".join(str((f or {}).get("text") or "") for f in (block.get("fields") or []))
    if kind == "context":
        return " ".join(str((e or {}).get("text") or "") for e in (block.get("elements") or []))
    if kind == "actions":
        return " ".join(
            str((el.get("text") or {}).get("text") or "")
            for el in (block.get("elements") or []))
    return ""


class SlackCardRenderer(CardRenderer):
    """Card IR → Slack Block Kit 载荷。"""

    def render(self, card: Card) -> RenderedCard:
        degraded: list[str] = []
        blocks: list[dict] = []
        hints: list[str] = []
        pending: list[Action] = []
        row: list[dict] = []
        button_count = 0

        def add(block: dict) -> None:
            blocks.append(block)

        def flush_row() -> None:
            if row:
                add(actions_block(list(row)))
                row.clear()

        def flush_actions() -> None:
            nonlocal button_count
            if not pending:
                return
            actions, pending[:] = list(pending), []
            for action in actions:
                if button_count >= MAX_ACTION_ELEMENTS:
                    hints.append(action_hint(action))
                    if "card_action_unsupported" not in degraded:
                        degraded.append("card_action_unsupported")
                    continue
                button_count += 1
                row.append(button_element(action))
                if len(row) >= _BUTTONS_PER_ROW:
                    flush_row()
            flush_row()

        if card.title:
            add(header_block(card.title))

        for block in card.blocks:
            if block.kind == "text":
                if block.text:
                    flush_actions()
                    for section in section_blocks(format_mrkdwn(block.text)):
                        add(section)
            elif block.kind == "fields":
                flush_actions()
                for section in fields_section(
                        (key, format_mrkdwn(value)) for key, value in block.fields):
                    add(section)
            elif block.kind == "divider":
                flush_actions()
                add(divider_block())
            elif block.kind == "select":
                if block.placeholder:
                    flush_actions()
                    add(section_block(format_mrkdwn(block.placeholder)))
                pending.extend(block.options)
            elif block.kind == "actions":
                pending.extend(block.options)
            elif block.kind == "button" and block.action is not None:
                pending.append(block.action)

        pending.extend(card.actions)
        # 底部按钮区在页脚之前（页脚是 context）
        flush_actions()

        if hints:
            add(section_block("\n".join(hints)))
        if card.page:
            add(context_block(f"第 {card.page[0]}/{card.page[1]} 页"))
        if card.hint:
            add(context_block(card.hint))

        if len(blocks) > MAX_BLOCKS:
            # 超出 50 blocks：保留 MAX_BLOCKS-1 个块，最后一块汇总溢出内容（不静默丢）。
            overflow, blocks[:] = blocks[MAX_BLOCKS - 1:], blocks[:MAX_BLOCKS - 1]
            summary = "\n".join(t for t in (_block_text(b) for b in overflow) if t)
            budget = MAX_SECTION_CHARS - len(_OVERFLOW_TRUNCATED_HINT)
            if len(summary) > budget:
                summary = summary[:budget] + _OVERFLOW_TRUNCATED_HINT
            if summary.strip():
                blocks.append(section_block(summary))
            if "card_text_fallback" not in degraded:
                degraded.append("card_text_fallback")

        if not blocks:
            # 空卡片：返回一段降级文本（egress 空载荷护栏会据此计 failed，不静默）
            text = render_text(card)
            if text.strip():
                blocks.append(section_block(format_mrkdwn(text)))
            else:
                degraded.append("card_text_fallback")

        return RenderedCard(payload={"blocks": blocks}, degraded=degraded)


__all__ = ["SlackCardRenderer"]
