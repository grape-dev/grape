"""CardRenderer —— Card IR → 平台形态（Issue #1095 渲染契约）。

每渠道一个 renderer：飞书（lark JSON）/ 钉钉（actionCard）。
共享文本降级渲染在 card.render_text（无 RICH_CARD 渠道由 egress 调用）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..card import Card


@dataclass
class RenderedCard:
    """渲染结果：payload 为平台载荷（dict）；degraded 收集降级原因（计 metrics）。"""

    payload: Any
    degraded: list[str] = field(default_factory=list)


class CardRenderer:
    """renderer 基类：render(Card) → RenderedCard（纯函数，可替换/可 A/B）。"""

    def render(self, card: Card) -> RenderedCard:  # pragma: no cover - 接口
        raise NotImplementedError


__all__ = ["CardRenderer", "RenderedCard"]
