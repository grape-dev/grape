"""飞书 CardRenderer：Card IR → lark 卡片 JSON（Issue #1095）。

外观零漂移契约（tests/im/test_card_ir.py 快照断言）：
  - v1：header(template) + elements（div/lark_md、action 行 button+value、hr、select_static）；
  - v2：schema 2.0 + body.elements（内联 button+behaviors、select_static）；
  - 回调载荷 = action_payload(Action)（与迁移前 value 字典完全一致）。
"""
from __future__ import annotations

import json
from typing import Any

from ..card import Action, ButtonStyle, Card, Tone, action_payload
from . import CardRenderer, RenderedCard

_TONE_TEMPLATE = {
    Tone.DEFAULT: "blue",
    Tone.INFO: "blue",
    Tone.NEUTRAL: "grey",
    Tone.SUCCESS: "green",
    Tone.WARNING: "yellow",
    Tone.DANGER: "red",
}


def _plain(content: str) -> dict:
    return {"tag": "plain_text", "content": content}


def _value_dict(action: Action) -> dict:
    return action_payload(action)


def _select_value(action: Action) -> str:
    return json.dumps(_value_dict(action), ensure_ascii=False)


class FeishuCardRenderer(CardRenderer):
    """Card IR → 飞书 lark 卡片 JSON（v1 elements / v2 schema 2.0）。"""

    def render(self, card: Card) -> RenderedCard:
        if card.form == "v2":
            return RenderedCard(payload=self._render_v2(card))
        return RenderedCard(payload=self._render_v1(card))

    # ---------------- v1

    def _render_v1(self, card: Card) -> dict:
        elements: list[dict] = []
        for block in card.blocks:
            if block.kind == "text":
                if block.text:
                    elements.append({"tag": "div",
                                     "text": {"tag": "lark_md", "content": block.text}})
            elif block.kind == "fields":
                if block.fields:
                    elements.append({"tag": "div", "fields": [
                        {"is_short": True, "text": {"tag": "lark_md",
                                                    "content": f"**{k}**\n{v}"}}
                        for k, v in block.fields]})
            elif block.kind == "divider":
                elements.append({"tag": "hr"})
            elif block.kind == "select":
                elements.append(self._v1_select(block.placeholder, block.options))
            elif block.kind == "actions":
                elements.append({"tag": "action",
                                 "actions": [self._v1_button(a) for a in block.options]})
            elif block.kind == "button":
                elements.append({"tag": "action",
                                 "actions": [self._v1_button(block.action)]})
        if card.actions:
            elements.append({"tag": "action",
                             "actions": [self._v1_button(a) for a in card.actions]})
        return {
            "header": {"title": _plain(card.title),
                       "template": _TONE_TEMPLATE.get(card.tone, "blue")},
            "elements": elements,
        }

    def _v1_button(self, action: Action) -> dict:
        btn: dict[str, Any] = {
            "tag": "button",
            "text": _plain(action.label or action.id),
            "value": _value_dict(action),
        }
        if action.style == ButtonStyle.PRIMARY:
            btn["type"] = "primary"
        elif action.style == ButtonStyle.DANGER:
            btn["type"] = "danger"
        return btn

    def _v1_select(self, placeholder: str, options: tuple[Action, ...]) -> dict:
        return {
            "tag": "select_static",
            "placeholder": _plain(placeholder),
            "options": [{"text": _plain(o.label or o.id), "value": _select_value(o)}
                        for o in options],
        }

    # ---------------- v2（schema 2.0：内联按钮 + behaviors）

    def _render_v2(self, card: Card) -> dict:
        body: list[dict] = []
        for block in card.blocks:
            if block.kind == "text":
                if block.text:
                    body.append({"tag": "div",
                                 "text": {"tag": "lark_md", "content": block.text}})
            elif block.kind == "fields":
                if block.fields:
                    body.append({"tag": "div", "fields": [
                        {"is_short": True, "text": {"tag": "lark_md",
                                                    "content": f"**{k}**\n{v}"}}
                        for k, v in block.fields]})
            elif block.kind == "divider":
                body.append({"tag": "hr"})
            elif block.kind == "select":
                body.append(self._v1_select(block.placeholder, block.options))
            elif block.kind == "actions":
                body.extend(self._v2_button(a) for a in block.options)
            elif block.kind == "button" and block.action is not None:
                body.append(self._v2_button(block.action))
        body.extend(self._v2_button(a) for a in card.actions)
        return {
            "schema": "2.0",
            "header": {"title": _plain(card.title),
                       "template": _TONE_TEMPLATE.get(card.tone, "blue")},
            "body": {"elements": body},
        }

    def _v2_button(self, action: Action) -> dict:
        btn: dict[str, Any] = {
            "tag": "button",
            "text": _plain(action.label or action.id),
            "behaviors": [{"type": "callback", "value": _value_dict(action)}],
        }
        if action.style == ButtonStyle.PRIMARY:
            btn["type"] = "primary"
        elif action.style == ButtonStyle.DANGER:
            btn["type"] = "danger"
        return btn


__all__ = ["FeishuCardRenderer"]
