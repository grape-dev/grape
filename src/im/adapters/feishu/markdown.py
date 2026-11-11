"""飞书 markdown 渲染 —— 复用既有 src/im/lark_md（A 资产，不重写）。"""
from __future__ import annotations

from src.im.lark_md import to_lark_md as _to_lark


def render_markdown(text: str) -> str:
    """markdown → 飞书卡片 lark_md。lark_md 是正确起点（§4.4），原样复用。"""
    return _to_lark(text)


__all__ = ["render_markdown"]
