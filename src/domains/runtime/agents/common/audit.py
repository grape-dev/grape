"""审计横切关注点——Protocol + 值对象 + Null 默认值。

横跨 policy 和 sandbox 的横切关注点，不属于任何一边。
common/ 是零依赖叶子——禁止 import policy/sandbox。

NullAuditLogger 放这里是因为它是默认值（消费方立刻能用），
不是抽象的一部分。零依赖零副作用。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol


class AuditKind(Enum):
    SANDBOX_ACCESS = "sandbox_access"
    NETWORK_DECISION = "network_decision"
    CREDENTIAL_INJECT = "credential_inject"
    COMMAND_GUARD = "command_guard"
    POLICY_DECISION = "policy_decision"
    SECURITY_ALERT = "security_alert"


@dataclass
class AuditEvent:
    ts: str
    task_id: str
    kind: AuditKind
    outcome: str  # allow / deny / degraded / stopped
    host: str = ""
    method: str = ""
    path: str = ""
    detail: dict = field(default_factory=dict)


class AuditListener(Protocol):
    """审计监听者——横切关注点。

    common/audit.py 定义 Protocol。
    sandbox/audit.py 的 FileAuditLogger 实现此 Protocol。
    PolicyManager 从 common.audit 导入，直接写审计。
    """

    def log(self, event: AuditEvent) -> None:
        """接收并记录审计事件。"""
        ...


class NullAuditLogger:
    """空审计——默认值。完全实现 Protocol，不抛 NotImplementedError。

    放 common/ 是因为消费方需要"什么都不做"时立刻能用。
    零依赖零副作用。
    """

    def log(self, event: AuditEvent) -> None:
        pass


def now_iso() -> str:
    """当前时间 ISO 格式。供各消费方构造 AuditEvent 时使用。"""
    return datetime.now(timezone.utc).isoformat()
