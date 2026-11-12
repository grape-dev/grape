"""能力位掩码（Issue #742 §3.3）——每渠道申报「我会什么」，声明不协商（无中央协商）。"""
from __future__ import annotations

import enum


class Capability(enum.IntFlag):
    TEXT = 1 << 0
    RICH_CARD = 1 << 1
    THREAD_REPLY = 1 << 2
    QUOTE_REPLY = 1 << 3
    ATTACHMENT = 1 << 4
    TYPING_INDICATOR = 1 << 5
    MESSAGE_EDIT = 1 << 6
    REACTION = 1 << 7     # Issue #1008：表情回执能力（飞书有 / 钉钉 webhook 无）
    CARD_ACTION = 1 << 8  # Issue #1095：能回收卡片动作回调（飞书有 / 钉钉批5 前无）；
                          # RICH_CARD 语义收紧为「能渲染 Card IR」


ALL_CAPABILITIES = tuple(Capability)


def describe(caps: Capability) -> list[str]:
    return [m.name for m in ALL_CAPABILITIES if caps & m]


__all__ = ["Capability", "ALL_CAPABILITIES", "describe"]
