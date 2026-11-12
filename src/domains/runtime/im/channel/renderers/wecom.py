"""企微 CardRenderer：Card IR → 模板卡片（Issue #1254 批 3）。

渲染契约（`channel/renderers/__init__.py` RenderedCard）：
  payload 是最终交给 sender 的平台载荷，degraded 收集降级原因（计 metrics）。

企微载荷形状（本渠道约定，sender 依此分流）：

    {
      "msgtype": "markdown",              # 兜底承载：egress 的空载荷护栏按 markdown 判定，
      "markdown": {"text": "<压平文本>"},  # 无模板/模板被拒时仍能发出去（内容不丢）
      "template_card": {...}               # 可选：模板卡片结构（sender 优先用它）
    }

要点：
  - 业务层零改动：Asset IR / Card IR 不变，平台字段只出现在本文件与 wecom/cards.py；
  - 有模板 → 真回调按钮（点击回 `template_card_event`，带操作人 userid）；
  - 无模板（`GRAPE_WECOM_TEMPLATE_CARD=off` 或结构为空）→ markdown 降级 +
    `card_action_unsupported`，按钮变「回复 `/cmd`」提示，动作语义不丢。
"""
from __future__ import annotations

import logging

from ..card import Card, render_text
from . import CardRenderer, RenderedCard
from ...adapters.wecom.cards import build_template_card, template_card_enabled

log = logging.getLogger(__name__)


class WecomCardRenderer(CardRenderer):
    """Card IR → 企微模板卡片载荷（msgtype=template_card + markdown 兜底）。"""

    def __init__(self, template_enabled: bool | None = None) -> None:
        self._template_enabled = (template_card_enabled()
                                  if template_enabled is None else bool(template_enabled))

    def render(self, card: Card) -> RenderedCard:
        degraded: list[str] = []
        text = render_text(card)
        payload: dict = {"msgtype": "markdown", "markdown": {"text": text}}

        if not self._template_enabled:
            payload["degraded"] = ["card_action_unsupported"]
            return RenderedCard(payload=payload, degraded=["card_action_unsupported"])

        try:
            rendered = build_template_card(card)
        except Exception as e:   # 渲染异常不得炸出站链路 → markdown 降级
            log.warning("[wecom] template card build failed: %r", e)
            payload["degraded"] = ["card_action_unsupported"]
            return RenderedCard(payload=payload,
                                degraded=["card_action_unsupported"])

        template = rendered.get("template_card") or {}
        if not template.get("main_title") and not template.get("sub_title_text"):
            payload["degraded"] = ["card_action_unsupported"]
            return RenderedCard(payload=payload,
                                degraded=["card_action_unsupported"])

        payload["template_card"] = template
        if card.actions and not template.get("button_list"):
            # 有卡片级动作但渲染不出按钮（如只有 url 动作走 jump_list）：交互降级为回复命令
            degraded.append("card_action_unsupported")
        return RenderedCard(payload=payload, degraded=degraded)


__all__ = ["WecomCardRenderer"]