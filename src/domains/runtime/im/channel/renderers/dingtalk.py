"""钉钉 CardRenderer：Card IR → actionCard（Issue #1095 批4）。

钉钉机器人 actionCard 交互边界（design §8）：按钮只支持 URL 跳转；
带 params 的语义动作按钮无回调通道（批5 才谈回调）→ 降级为
markdown 命令提示行并计 degraded("card_action_unsupported")，
不出空按钮、不静默丢动作。
"""
from __future__ import annotations

from ..card import Action, Card, action_payload, hard_breaks, numbered_option_label
from . import CardRenderer, RenderedCard


class DingtalkCardRenderer(CardRenderer):
    """Card IR → 钉钉 actionCard 载荷（webhook msgtype=actionCard）。"""

    def render(self, card: Card) -> RenderedCard:
        degraded: list[str] = []
        text_lines: list[str] = []
        if card.title:
            text_lines.append(f"### {card.title}")
        btns: list[dict[str, str]] = []
        all_actions: list[Action] = []

        for block in card.blocks:
            if block.kind == "text":
                if block.text:
                    text_lines.append(hard_breaks(block.text))
            elif block.kind == "fields":
                for key, value in block.fields:
                    text_lines.append(f"- {key}: {value}")
            elif block.kind == "divider":
                text_lines.append("---")
            elif block.kind == "select":
                if block.placeholder:
                    text_lines.append(block.placeholder)
                for i, opt in enumerate(block.options, 1):
                    text_lines.append(numbered_option_label(i, opt.label))
            elif block.kind == "actions":
                all_actions.extend(block.options)
            elif block.kind == "button" and block.action is not None:
                all_actions.append(block.action)
        all_actions.extend(card.actions)

        for action in all_actions:
            if action.url:
                btns.append({"title": action.label or action.id, "actionURL": action.url})
            else:
                # 无回调通道：动作降级为命令提示（内容不丢，计 degraded）
                cmd = action_payload(action).get("cmd", "")
                text_lines.append(f"**{action.label or action.id}**：回复 `{cmd}`")
                degraded.append("card_action_unsupported")

        if card.page:
            text_lines.append(f"第 {card.page[0]}/{card.page[1]} 页")
        if card.hint:
            text_lines.append(card.hint)

        payload = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": card.title or "grape",
                "text": "\n\n".join(text_lines) or card.title or "grape",
                "btns": btns,
                "btnOrientation": "0",
            },
        }
        if not btns:
            payload["actionCard"].pop("btns")
            payload["actionCard"].pop("btnOrientation")
        return RenderedCard(payload=payload, degraded=degraded)


__all__ = ["DingtalkCardRenderer"]
