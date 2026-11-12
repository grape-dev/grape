"""Slack Block Kit 组装原语 + 回调解析（Issue #1309 §3.4 / §4.4）。

平台上限（官方文档，硬约束）：
  - 单条消息最多 **50 blocks**；
  - `section.text` 最多 **3000 字符**；
  - `section.fields` 中每个字段最多 **2000 字符**；
  - 单个 `actions` block 最多 **25 个元素**。

本模块只做"块级拼装 + 上限切分"，Card IR → blocks 的业务映射在
`src/im/channel/renderers/slack.py`（分层契约：平台字段只出现在适配器与 renderer）。

按钮回调：点击 → `block_actions` → `actions[0].action_id` + `value`（JSON 串）
+ `user.id`。`value` 就是 `action_payload()` 的序列化（`{"cmd": "/bind 2"}`），
回收后与"回数字"走同一份 `_handle_selection`。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from ...channel.card import Action, ButtonStyle, action_payload

MAX_BLOCKS = 50
MAX_SECTION_CHARS = 3000
MAX_FIELD_CHARS = 2000
MAX_ACTION_ELEMENTS = 25
MAX_FIELDS_PER_SECTION = 10
MAX_BUTTON_TEXT = 75


def header_block(title: str) -> dict:
    """卡片标题 → header block（plain_text ≤150 字符）。"""
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": (title or "").strip()[:150] or "grape"},
    }


def section_block(text: str) -> dict:
    """mrkdwn 文本段（调用方保证 ≤3000 字符；超长用 section_blocks 切分）。"""
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": (text or "")[:MAX_SECTION_CHARS]},
    }


def section_blocks(text: str) -> list[dict]:
    """长文本 → 多个 section（按 3000 字符切，保留换行与代码围栏）。"""
    from .markdown import split_text

    if not (text or "").strip():
        return []
    return [section_block(piece) for piece in split_text(text, MAX_SECTION_CHARS)]


def fields_section(pairs: Iterable[tuple[str, str]]) -> list[dict]:
    """字段对 → section.fields（每字段 ≤2000 字符，每段最多 10 个字段）。

    单个字段超长时拆成连续多个字段（不丢内容），再按 10 个/段分组。
    """
    items: list[str] = []
    for key, value in (pairs or []):
        if not (key or value):
            continue
        item = f"*{key}*: {value}"
        for start in range(0, len(item), MAX_FIELD_CHARS):
            items.append(item[start:start + MAX_FIELD_CHARS])
    if not items:
        return []
    blocks: list[dict] = []
    for start in range(0, len(items), MAX_FIELDS_PER_SECTION):
        chunk = items[start:start + MAX_FIELDS_PER_SECTION]
        blocks.append({
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": item} for item in chunk],
        })
    return blocks


def divider_block() -> dict:
    return {"type": "divider"}


def context_block(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": (text or "")[:3000]}]}


def button_element(action: Action) -> dict:
    """语义动作 → Block Kit button（url 动作直接跳转；其余带 action_id + value）。"""
    label = (action.label or action.id or "").strip() or "操作"
    text = {"type": "plain_text", "text": label[:MAX_BUTTON_TEXT]}
    if action.url:
        return {"type": "button", "text": text, "url": action.url}
    element: dict[str, Any] = {
        "type": "button",
        "text": text,
        "action_id": (action.id or "action")[:255],
        "value": json.dumps(action_payload(action), ensure_ascii=False)[:2000],
    }
    style = {ButtonStyle.PRIMARY: "primary", ButtonStyle.DANGER: "danger"}.get(action.style)
    if style:
        element["style"] = style
    return element


def actions_block(elements: list[dict]) -> dict:
    """按钮行（调用方保证 ≤25 个元素；超限由 renderer 降级为文本提示）。"""
    return {"type": "actions", "elements": elements[:MAX_ACTION_ELEMENTS]}


def action_command(action: Action) -> str:
    """动作 → 用户可回复的命令串（降级提示用）。"""
    return str(action_payload(action).get("cmd", "") or "")


def action_hint(action: Action) -> str:
    """降级提示：按钮语义保留为「回复 `/cmd`」一行（不静默丢动作）。"""
    label = (action.label or action.id or "").strip() or "操作"
    if action.url:
        return f"• {label}: {action.url}"
    return f"• {label}: 回复 `{action_command(action)}`"


def parse_action_value(value: Any) -> dict:
    """block_actions 的 value → 载荷字典（JSON 串容错；纯命令串回落 {"cmd": ...}）。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {"cmd": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"cmd": raw}
    return {}


def command_from_value(value: Any, action_id: str = "") -> str:
    """回调 value → 命令串（与引擎 parse_command 契约一致：以 "/" 开头）。"""
    payload = parse_action_value(value)
    cmd = str(payload.get("cmd") or "").strip()
    if cmd:
        return cmd
    if action_id:
        return "/" + action_id.lstrip("/")
    return ""


def card_token(channel: str, ts: str) -> str:
    """原地更新令牌：`<channel>|<ts>`（chat.update 需要这两个字段）。"""
    return f"{channel or ''}|{ts or ''}"


def parse_card_token(token: str) -> tuple[str, str]:
    channel, _, ts = (token or "").partition("|")
    return channel, ts


__all__ = [
    "MAX_ACTION_ELEMENTS",
    "MAX_BLOCKS",
    "MAX_FIELD_CHARS",
    "MAX_SECTION_CHARS",
    "action_command",
    "action_hint",
    "actions_block",
    "button_element",
    "card_token",
    "command_from_value",
    "context_block",
    "divider_block",
    "fields_section",
    "header_block",
    "parse_action_value",
    "parse_card_token",
    "section_block",
    "section_blocks",
]
