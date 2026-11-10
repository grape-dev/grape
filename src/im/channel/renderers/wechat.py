"""微信 CardRenderer：Card IR → 纯文本「标题 + 正文 + 编号选项」（Issue #1728 M3）。

微信 ilink 通道**没有卡片、没有按钮、没有回调**（能力声明仅 direct/media/
blockStreaming），因此本 renderer 是唯一形态：

    【标题】
    正文行…
    1. 选项一
    2. 选项二
    回复数字选择

映射（业务层零改动）：
  - `title`   → `【标题】` 首行；
  - `text`    → 原样正文（/repo 卡片正文自带 1..N 编号列表）；
  - `fields`  → `键：值` 行；
  - `divider` → `──────` 分隔行；
  - `select`  → 占位行 + 编号选项；
  - 动作类块/`card.actions`：带 `num` 参数或纯数字 label 的选项已在正文编号，
    不重复编号；其余动作降级为「回复 `/cmd`」提示；url 动作输出链接；
  - `page`    → `第 x/y 页`；`hint` → 末行提示。

用户回数字后由既有 `_handle_selection`（pending selection）承担选择，
本渠道**不新增**第二条选择逻辑（避免路径漂移）。
"""
from __future__ import annotations

from ..card import Action, Card, action_payload, numbered_option_label, render_text
from . import CardRenderer, RenderedCard

_DIVIDER = "──────"


def _is_numeric_selection(action: Action) -> bool:
    label = (action.label or "").strip()
    return action.params.get("num") is not None or label.isdigit()


def _action_hint(action: Action) -> str:
    label = (action.label or action.id or "").strip() or "操作"
    if action.url:
        return f"{label}：{action.url}"
    cmd = action_payload(action).get("cmd", "")
    return f"{label}：回复 `{cmd}`"


class WechatCardRenderer(CardRenderer):
    """Card IR → 微信纯文本载荷（`{"text": ...}`）。"""

    def render(self, card: Card) -> RenderedCard:
        degraded: list[str] = []
        lines: list[str] = []
        pending: list[Action] = []
        has_selection = False

        if card.title:
            lines.append(f"【{card.title}】")

        for block in card.blocks:
            if block.kind == "text":
                if block.text:
                    lines.append(block.text)
            elif block.kind == "fields":
                for key, value in block.fields:
                    lines.append(f"{key}：{value}")
            elif block.kind == "divider":
                lines.append(_DIVIDER)
            elif block.kind == "select":
                if block.placeholder:
                    lines.append(block.placeholder)
                for i, option in enumerate(block.options, 1):
                    lines.append(numbered_option_label(
                        i, option.label or action_payload(option).get("cmd", "")))
                    if _is_numeric_selection(option):
                        has_selection = True
            elif block.kind == "actions":
                pending.extend(block.options)
            elif block.kind == "button" and block.action is not None:
                pending.append(block.action)

        pending.extend(card.actions)

        for action in pending:
            if _is_numeric_selection(action):
                has_selection = True
                continue
            lines.append(_action_hint(action))

        if has_selection:
            lines.append("回复数字选择")
        if card.page:
            lines.append(f"第 {card.page[0]}/{card.page[1]} 页")
        if card.hint:
            lines.append(card.hint)

        text = "\n".join(line for line in lines if line).strip()
        if not text:
            text = render_text(card)
            degraded.append("card_text_fallback")

        return RenderedCard(payload={"text": text}, degraded=degraded)


__all__ = ["WechatCardRenderer"]