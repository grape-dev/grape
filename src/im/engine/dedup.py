"""入站去重（Issue #742 §4.1 步骤③）——两阶段化（继承 A3 跨重启持久化）。

阶段 1 claim：`try_claim` 处理前抢占（幂等键 = installation + message_id）；
阶段 2 mark：`mark` 与消息持久化**同事务**提交（崩溃重投不丢不重）。
基础设施异常时 `release` 归还 claim（重投递是仅剩机会）。

兼容旧表：P0 双写期读 bot_seen_messages（单阶段语义：见到即处理过），
新表 grape_t_channel_inbound_dedup 接管后两阶段生效。
"""
from __future__ import annotations

import enum
import time
import uuid
from typing import Optional, Protocol

from ..channel.message import InboundMessage


class Verdict(str, enum.Enum):
    CLAIM = "claim"      # 首次见到，继续处理
    DROP = "drop"        # 已处理/已 claim，丢弃
    LEGACY_SEEN = "legacy_seen"  # 旧表已见（双写期对账用）


class DedupStore(Protocol):
    def try_claim(self, installation_id: str, message_id: str, token: str) -> bool: ...
    def mark(self, installation_id: str, message_id: str) -> bool: ...
    def release(self, installation_id: str, message_id: str) -> None: ...
    def seen(self, installation_id: str, message_id: str) -> bool: ...


class InMemoryDedupStore:
    """测试/单副本替身。"""

    def __init__(self) -> None:
        self._rows: dict[tuple, tuple] = {}  # (inst, mid) -> (state, token)

    def try_claim(self, installation_id: str, message_id: str, token: str) -> bool:
        key = (installation_id, message_id)
        if key in self._rows:
            return False
        self._rows[key] = ("claimed", token)
        return True

    def mark(self, installation_id: str, message_id: str) -> bool:
        key = (installation_id, message_id)
        if key not in self._rows:
            return False
        state, token = self._rows[key]
        self._rows[key] = ("marked", token)
        return True

    def release(self, installation_id: str, message_id: str) -> None:
        self._rows.pop((installation_id, message_id), None)

    def seen(self, installation_id: str, message_id: str) -> bool:
        return (installation_id, message_id) in self._rows


class InboundDedup:
    """两阶段去重器。installation_id 为空时退化为 message_id 全局键（P0 双写期）。"""

    def __init__(self, store: DedupStore) -> None:
        self._store = store
        self._tokens: dict[tuple, str] = {}

    def try_claim(self, installation_id: str, msg: InboundMessage) -> Verdict:
        mid = msg.message_id or msg.event_id
        if self._store.seen(installation_id, mid):
            return Verdict.DROP
        token = str(uuid.uuid4())
        if not self._store.try_claim(installation_id, mid, token):
            return Verdict.DROP
        self._tokens[(installation_id, mid)] = token
        return Verdict.CLAIM

    def mark(self, installation_id: str, msg: InboundMessage) -> None:
        mid = msg.message_id or msg.event_id
        self._store.mark(installation_id, mid)
        self._tokens.pop((installation_id, mid), None)

    def release(self, installation_id: str, msg: InboundMessage) -> None:
        mid = msg.message_id or msg.event_id
        self._store.release(installation_id, mid)
        self._tokens.pop((installation_id, mid), None)


__all__ = ["Verdict", "DedupStore", "InMemoryDedupStore", "InboundDedup"]
