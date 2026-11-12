"""Telegram InlineKeyboard 组装 + callback_data 短码编解码（Issue #1613 §3.4）。

Telegram 的 `callback_data` 只有 **1–64 字节（UTF-8）**，这是本渠道唯一的硬设计约束：
  - 常规动作压成短码 `verb:参数...`，如 `b:2`（bind 第 2 项）、`h`（help）、
    `fd:12:34`（finding-dispatch 的 exec_id:finding_id）；
  - 短码放不下（如 finding-scene 带 UUID scene_id）→ 生成 `c:<cid>`（8 位 base36），
    完整 action_payload 记入 `TelegramCardLedger`（内存 + 可选持久化钩子），回调按 cid 回查；
  - 映射表也放不下 → 编码返回 None，渲染层把按钮降级为「回复 `/命令`」文本，
    计 `card_action_unsupported`（不静默丢动作）。

按钮上限与 Slack 对齐：**总数 ≤25，每行 ≤8**（官方无总数硬上限，但消息体积与体验在此
附近显著下降）。超出部分转文本提示。
"""
from __future__ import annotations

import itertools
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from ...channel.card import Action, action_payload

MAX_CALLBACK_BYTES = 64
MAX_BUTTONS = 25
MAX_BUTTONS_PER_ROW = 8
MAX_BUTTON_TEXT = 64
LEDGER_MAX_ENTRIES = 2000

# 语义动作 id → 短码 verb（未知 id 优先走台账）
_VERBS = {
    "help": "h",
    "repo": "r",
    "chat": "ch",
    "task": "t",
    "new": "n",
    "where": "w",
    "cancel": "c",
    "bind": "b",
    "join": "j",
    "finding-list": "fl",
    "finding-select": "fs",
    "finding-scene": "fsc",
    "finding-rescene": "fr",
    "finding-dispatch": "fd",
}
_VERB_TO_ID = {verb: action_id for action_id, verb in _VERBS.items()}

# 无位置参数动作的参数顺序（短码按此顺序拼接，decode 时还原）
_PARAM_ORDER = {
    "finding-list": ("exec_id",),
    "finding-select": ("exec_id", "finding_id"),
    "finding-scene": ("exec_id", "finding_id", "scene_id"),
    "finding-rescene": ("exec_id", "finding_id"),
    "finding-dispatch": ("exec_id", "finding_id", "scene_id"),
}
_POSITIONAL_IDS = frozenset({"bind", "join"})

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(value: int) -> str:
    if value <= 0:
        return "0"
    digits: list[str] = []
    while value:
        value, rem = divmod(value, 36)
        digits.append(_B36[rem])
    return "".join(reversed(digits))


_DEFAULT_LEDGER: "TelegramCardLedger | None" = None


def default_ledger() -> "TelegramCardLedger":
    """进程级共享台账：renderer 写入的 cid 由 channel 回调侧回查。"""
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        _DEFAULT_LEDGER = TelegramCardLedger()
    return _DEFAULT_LEDGER


class TelegramCardLedger:
    """cid → 完整 action_payload 台账（进程内有界；可选持久化钩子）。"""

    def __init__(self, max_entries: int = LEDGER_MAX_ENTRIES,
                 persist: Callable[[str, dict], None] | None = None,
                 resolve: Callable[[str], dict | None] | None = None) -> None:
        self._max = max(1, int(max_entries))
        self._persist = persist
        self._resolve = resolve
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._map: dict[str, tuple[dict, float]] = {}

    def put(self, payload: dict) -> str:
        payload = dict(payload or {})
        with self._lock:
            cid = _base36(next(self._seq))
            if len(self._map) >= self._max:
                oldest = min(self._map, key=lambda k: self._map[k][1])
                self._map.pop(oldest, None)
            self._map[cid] = (payload, time.monotonic())
        if self._persist is not None:
            try:
                self._persist(cid, payload)
            except Exception:
                pass
        return cid

    def get(self, cid: str) -> dict:
        with self._lock:
            hit = self._map.get(cid or "")
        if hit is not None:
            return dict(hit[0])
        if self._resolve is not None:
            try:
                found = self._resolve(cid or "")
            except Exception:
                found = None
            if isinstance(found, dict):
                with self._lock:
                    self._map[cid] = (dict(found), time.monotonic())
                return dict(found)
        return {}

    def __len__(self) -> int:
        return len(self._map)


def _encode_simple(action: Action) -> str | None:
    """尝试生成完整语义短码；放不下返回 None（调用方回落台账/降级）。"""
    verb = _VERBS.get(action.id)
    if verb is None:
        return None
    params = dict(action.params or {})
    if action.id in _POSITIONAL_IDS:
        num = params.get("num")
        return f"{verb}:{num}" if num is not None else verb
    order = _PARAM_ORDER.get(action.id)
    if order is None:
        return verb if not params else None
    if not set(params).issubset(set(order)):
        return None
    parts = [verb]
    for key in order:
        if key not in params:
            break
        parts.append(urllib.parse.quote(str(params[key]), safe=""))
    return ":".join(parts)


def encode_callback_data(action: Action,
                         ledger: TelegramCardLedger | None = None) -> str | None:
    """Action → callback_data（≤64 字节）；短码放不下则走台账；都放不下返回 None。"""
    if action.url:
        return None
    simple = _encode_simple(action)
    if simple is not None and len(simple.encode("utf-8")) <= MAX_CALLBACK_BYTES:
        return simple
    if ledger is not None:
        cid = ledger.put(action_payload(action))
        data = f"c:{cid}"
        if len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES:
            return data
    return None


def decode_callback_data(data: str,
                         ledger: TelegramCardLedger | None = None) -> dict:
    """callback_data → action_payload（{"cmd": ..., **params}）。

    无法识别返回空字典（调用方丢弃，不抛异常穿透轮询循环）。
    """
    raw = str(data or "")
    if not raw:
        return {}
    if raw.startswith("c:"):
        return ledger.get(raw[2:]) if ledger is not None else {}
    if raw.startswith("/"):
        return {"cmd": raw}
    parts = raw.split(":")
    verb, rest = parts[0], parts[1:]
    action_id = _VERB_TO_ID.get(verb)
    if action_id is None:
        return {}
    cmd = "/" + action_id
    payload: dict[str, Any] = {"cmd": cmd}
    if action_id in _POSITIONAL_IDS:
        if rest:
            cmd = f"{cmd} {urllib.parse.unquote(rest[0])}"
            payload["cmd"] = cmd
        return payload
    order = _PARAM_ORDER.get(action_id)
    if order is None:
        return payload
    for key, value in zip(order, rest):
        payload[key] = urllib.parse.unquote(value)
    return payload


def button_element(action: Action,
                   ledger: TelegramCardLedger | None = None) -> dict | None:
    """语义动作 → inline_keyboard 按钮；url 动作用 url 按钮；无法编码返回 None。"""
    label = (action.label or action.id or "").strip() or "操作"
    text = label[:MAX_BUTTON_TEXT]
    if action.url:
        return {"text": text, "url": action.url}
    data = encode_callback_data(action, ledger)
    if data is None:
        return None
    return {"text": text, "callback_data": data}


def action_command(action: Action) -> str:
    return str(action_payload(action).get("cmd", "") or "")


def action_hint(action: Action) -> str:
    label = (action.label or action.id or "").strip() or "操作"
    if action.url:
        return f"• {label}: {action.url}"
    return f"• {label}: 回复 `{action_command(action)}`"


def inline_keyboard(actions: list[Action],
                    ledger: TelegramCardLedger | None = None
                    ) -> tuple[list[list[dict]], list[str]]:
    """动作列表 → (inline_keyboard 行, 降级提示行)。总数 ≤25、每行 ≤8。"""
    rows: list[list[dict]] = []
    hints: list[str] = []
    row: list[dict] = []
    count = 0
    for action in actions:
        if count >= MAX_BUTTONS:
            hints.append(action_hint(action))
            continue
        element = button_element(action, ledger)
        if element is None:
            hints.append(action_hint(action))
            continue
        row.append(element)
        count += 1
        if len(row) >= MAX_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows, hints


__all__ = [
    "LEDGER_MAX_ENTRIES",
    "MAX_BUTTONS",
    "MAX_BUTTONS_PER_ROW",
    "MAX_CALLBACK_BYTES",
    "TelegramCardLedger",
    "action_command",
    "action_hint",
    "button_element",
    "decode_callback_data",
    "default_ledger",
    "encode_callback_data",
    "inline_keyboard",
]