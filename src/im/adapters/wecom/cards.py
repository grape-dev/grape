"""企微模板卡片（Issue #1254 批 3）—— Card IR 的企微承载 + 回调解码 + 台账。

企微「智能机器人」的模板卡片不是「先在后台建模板再引用 template_id」，而是
**随消息内联声明 card_type + 结构体**（官方文档 /document/path/101463「模板卡片消息」）：

  - card_type = text_notice        纯展示（main_title / sub_title_text /
                                   horizontal_content_list / jump_list）
  - card_type = button_interaction 可点按钮（button_list[{text, style, key}] + task_id）
                                    点击回 `aibot_event_callback`，
                                    eventtype=template_card_event，
                                    template_card_event.event_key 即按钮 key

因此「模板卡片」能力是**真回调**（能拿操作人 userid、可按 task_id 原地更新），
与钉钉 actionCard 只能跳 URL 的退化路线不同 —— 这也是本渠道申报 CARD_ACTION 的依据。

降级口径：模板卡片被显式关闭（`GRAPE_WECOM_TEMPLATE_CARD=off` / 构造参数
template_enabled=False）或结构不合法时，渲染层退回 markdown（按钮变「回复 /cmd」
文本提示），计 `card_action_unsupported`，**不丢内容、不静默**。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ...channel.card import Action, Card, action_payload, numbered_option_label

log = logging.getLogger(__name__)

CARD_ENV = "GRAPE_WECOM_TEMPLATE_CARD"
MAX_BUTTONS = 6              # button_list 上限内再留余量（超出降级为文本提示）
SUB_TITLE_LIMIT = 1000       # sub_title_text 字节预算（保守，正文过长会被客户端截断）
TASK_TTL = 6 * 3600.0        # task_id 台账保留时长（覆盖 24h 回复窗口内的一次会话）


def template_card_enabled() -> bool:
    """模板卡片开关：默认开；off/0/false → 全渠道 markdown 降级（回滚开关）。"""
    env = os.environ.get(CARD_ENV, "on").strip().lower()
    return env not in ("off", "0", "false", "no")


def decode_action_key(key: str) -> dict[str, Any] | None:
    """按钮 key → 结构化动作载荷（渲染期写入的 action_payload 序列化）。"""
    if not key:
        return None
    try:
        value = json.loads(key)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def encode_action_key(action: Action) -> str:
    return json.dumps(action_payload(action), ensure_ascii=False, sort_keys=True)


def event_key_to_command(key: str) -> str:
    """按钮 key → 命令串（`/bind 2`）；非结构化 key 回落 `/<key>`（不丢动作语义）。"""
    value = decode_action_key(key)
    if isinstance(value, dict):
        cmd = str(value.get("cmd") or "").strip()
        if cmd:
            return cmd
    key = (key or "").strip()
    if not key:
        return ""
    return key if key.startswith("/") else f"/{key}"


# ---------------------------------------------------------------- 渲染

def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _truncate_bytes(text: str, limit: int) -> str:
    if _bytes(text) <= limit:
        return text
    encoded = text.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore").rstrip() + "…"


def _split_actions(card: Card) -> tuple[list[Action], list[Action]]:
    """动作分流：(可点按钮, 跳转链接)。

    select（选项列表）与 actions（按钮行）/ button / card.actions 都产出可点按钮：
    点击回调回 `action_payload`（`/bind 2`），与「回复数字」走同一份处理路径。
    """
    key_actions: list[Action] = []
    url_actions: list[Action] = []
    for block in card.blocks:
        if block.kind == "select":
            for opt in block.options:
                (url_actions if opt.url else key_actions).append(opt)
        elif block.kind == "actions":
            for act in block.options:
                (url_actions if act.url else key_actions).append(act)
        elif block.kind == "button" and block.action is not None:
            act = block.action
            (url_actions if act.url else key_actions).append(act)
    for act in card.actions:
        (url_actions if act.url else key_actions).append(act)
    return key_actions, url_actions


_CARD_ACTION_URL: str | None = None
_DEFAULT_CARD_URL = "https://www.grapedev.cn"


def _card_action_url() -> str:
    """企业微信 template_card 的 card_action 是**必填**字段（type=1 + url）。

    卡片自身没有跳转链接时用平台主页兜底：env GRAPE_PUBLIC_BASE_URL → meta.server_base_url
    → 现网默认域名。缺了这一项企微直接回 42045「card_action Missing or Invalid」。
    """
    global _CARD_ACTION_URL
    if _CARD_ACTION_URL:
        return _CARD_ACTION_URL
    url = (os.environ.get("GRAPE_PUBLIC_BASE_URL") or "").strip()
    if not url:
        try:
            from src.user.db import get_server_base_url
            url = (get_server_base_url() or "").strip()
        except Exception:
            url = ""
    _CARD_ACTION_URL = (url or _DEFAULT_CARD_URL).rstrip("/")
    return _CARD_ACTION_URL


def build_template_card(card: Card, *, task_id: str = "",
                        chat_id: str = "") -> dict[str, Any]:
    """Card IR → 企微 template_card 消息体（msgtype + template_card）。

    chat_id 只用于台账登记的可读性（task_id 才是卡片更新与回调关联键）。
    按钮数超出上限的部分降级为「标签：回复 `/cmd`」文本行，动作语义不丢。
    """
    key_actions, url_actions = _split_actions(card)
    buttons = key_actions[:MAX_BUTTONS]
    overflow = key_actions[MAX_BUTTONS:]

    lines: list[str] = []
    contents: list[dict[str, str]] = []
    for block in card.blocks:
        if block.kind == "text":
            if block.text:
                lines.append(block.text)
        elif block.kind == "fields":
            for key, value in block.fields:
                contents.append({"keyname": str(key), "value": str(value)})
        elif block.kind == "divider":
            lines.append("———")
        elif block.kind == "select":
            if block.placeholder:
                lines.append(block.placeholder)
            if len(buttons) + len(overflow) > MAX_BUTTONS:
                # 按钮不够用时把选项编号写进正文（文字兜底路径始终可用）
                for i, opt in enumerate(block.options, 1):
                    lines.append(numbered_option_label(
                        i, opt.label or action_payload(opt).get("cmd", "")))
    if card.page:
        lines.append(f"第 {card.page[0]}/{card.page[1]} 页")
    if card.hint:
        lines.append(card.hint)
    for act in overflow:
        lines.append(f"{act.label or act.id}：回复 `{action_payload(act).get('cmd', '')}`")

    card_type = "button_interaction" if buttons else "text_notice"
    template: dict[str, Any] = {
        "card_type": card_type,
        "main_title": {"title": card.title or "grape"},
    }
    sub_title = "\n".join(lines).strip()
    if sub_title:
        template["main_title"]["desc"] = _truncate_bytes(sub_title, 120)
        template["sub_title_text"] = _truncate_bytes(sub_title, SUB_TITLE_LIMIT)
    if contents:
        template["horizontal_content_list"] = contents
    if url_actions:
        template["jump_list"] = [
            {"type": 1, "title": (a.label or a.id)[:30], "url": a.url}
            for a in url_actions[:MAX_BUTTONS]
        ]
    if buttons:
        template["button_list"] = [
            {"text": (a.label or a.id or action_payload(a).get("cmd", ""))[:30],
             "style": _button_style(a), "key": encode_action_key(a)}
            for a in buttons
        ]
        template["task_id"] = task_id or uuid.uuid4().hex[:32]

    # 企微硬性要求：card_action 必填（否则 42045）。有链接动作就用它，没有用平台主页兜底。
    _action_url = str(url_actions[0].url or "") if url_actions else _card_action_url()
    if not _action_url:
        raise ValueError("wecom template_card requires card_action url")
    template["card_action"] = {"type": 1, "url": _action_url}

    return {"msgtype": "template_card", "template_card": template}


_STYLE_MAP = {"default": 1, "primary": 1, "danger": 2}


def _button_style(action: Action) -> int:
    """ButtonStyle → 企微 style（1 灰/2 红，语义 danger 优先）。"""
    style = getattr(action.style, "value", "default")
    return _STYLE_MAP.get(str(style), 1)


# ---------------------------------------------------------------- 台账

@dataclass
class CardRecord:
    chat_id: str
    chat_type: int = 1
    at: float = field(default_factory=time.monotonic)


class CardLedger:
    """task_id / 卡片键 → 会话反查（原地更新与回调归属用），容量有界 + TTL。"""

    def __init__(self, ttl: float = TASK_TTL, max_entries: int = 4096,
                 clock=time.monotonic) -> None:
        self._ttl = ttl
        self._max = max(1, int(max_entries))
        self._clock = clock
        self._map: dict[str, CardRecord] = {}

    def register(self, card_key: str, chat_id: str, chat_type: int = 1) -> None:
        if not card_key:
            return
        now = self._clock()
        expired = [k for k, v in self._map.items() if now - v.at > self._ttl]
        for k in expired:
            self._map.pop(k, None)
        if card_key not in self._map and len(self._map) >= self._max:
            oldest = min(self._map, key=lambda k: self._map[k].at)
            self._map.pop(oldest, None)
        self._map[card_key] = CardRecord(chat_id=chat_id, chat_type=chat_type, at=now)

    def lookup(self, card_key: str) -> CardRecord | None:
        record = self._map.get(card_key or "")
        if record is None:
            return None
        if self._clock() - record.at > self._ttl:
            self._map.pop(card_key, None)
            return None
        return record

    def __len__(self) -> int:
        return len(self._map)


_default_ledger = CardLedger()


def default_card_ledger() -> CardLedger:
    return _default_ledger


__all__ = [
    "CARD_ENV",
    "MAX_BUTTONS",
    "CardLedger",
    "CardRecord",
    "build_template_card",
    "decode_action_key",
    "default_card_ledger",
    "encode_action_key",
    "event_key_to_command",
    "template_card_enabled",
]