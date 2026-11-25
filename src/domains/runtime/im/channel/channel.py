"""Channel 接口 + ChannelConfig + 错误契约（Issue #742 §3.1）。

入站故意不在接口上：适配器在自己的接收循环里主动调用注入的
inbound_handler；核心绝不轮询 Channel（依赖倒置）。

InboundHandler 契约：返回异常仅代表基础设施失败（DB 挂了）；
产品性丢弃（去重命中/未绑定/群内未 @）不是异常——适配器不得因
产品丢弃触发重连。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

from .capability import Capability
from .message import InboundMessage, OutboundMessage, SendResult
from .types import Scope, Type


class ChannelError(Exception):
    """连接级失败：Supervisor 按指数退避重连。"""


class PermanentChannelError(ChannelError):
    """永久失败（凭证无效/app 不存在等）→ Supervisor 置 revoked 不再重试（继承 A5）。"""


class ChannelRegistrationConflict(ChannelError):
    """并发注册冲突：服务端对同一 app 的并发长连接数已达上限，拒绝本次注册。

    例：飞书 WS 关闭码 ``3001 (registered) too many connections``。
    仍属**可重试**的链路失败（继承 ChannelError），但同一时刻重连只会继续撞上限，
    因此 Supervisor 需按独立归因（`ws_register_conflict`）走更长退避并在连续多次
    失败后升级告警，而不是当作普通链路抖动每 15s 重试一次。
    """


class ChannelWsClosed(ChannelError):
    """传输层 WS 关闭：对端/服务端发来关闭帧（含关闭码）。

    例：飞书 ``3003 (registered) ping timeout``——服务端主动关闭，是**预期内的传输层
    抖动**，不是适配器 bug。携带关闭码让 Supervisor 按 ``ws_close_<code>`` 独立归因、
    记 WARNING 单行，而不是落进 ``channel crashed (unexpected)`` 全栈 ERROR（Issue #3795）。

    仍属可重试（继承 ChannelError → 既有指数退避重连）。
    """

    def __init__(self, message: str = "", close_code: Optional[int] = None) -> None:
        self.close_code = close_code
        super().__init__(message or f"ws closed code={close_code}")

    def __str__(self) -> str:
        base = super().__str__()
        if "ws closed code=" in base:
            return base
        return f"{base} (ws closed code={self.close_code})"


class CancelContext:
    """连接生命周期取消信号；必须容忍在新 ctx 上被重复调用（继承 run_bot 语义）。"""

    def __init__(self) -> None:
        import asyncio
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


InboundHandler = Callable[[CancelContext, InboundMessage], Awaitable[Optional[Exception]]]


@dataclass
class ChannelConfig:
    """安装配置的渠道视图。raw 不透明：核心永不读其内部。"""

    type: Type
    raw: dict[str, Any] = field(default_factory=dict)
    installation_id: UUID = UUID(int=0)
    scope: Scope = Scope.USER
    inbound_handler: Optional[InboundHandler] = None


class Channel(abc.ABC):
    """平台无关契约。"""

    @abc.abstractmethod
    def type(self) -> Type: ...

    @abc.abstractmethod
    async def connect(self, ctx: CancelContext) -> None:
        """阻塞运行接收循环直到 ctx 取消/链路断开。

        抛 ChannelError → Supervisor 按退避重连；
        抛 PermanentChannelError → 置 revoked 不再重试。
        """

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    async def send(self, out: OutboundMessage) -> SendResult: ...

    def capabilities(self) -> Capability:
        return Capability.TEXT

    def validate_config(self, raw: dict[str, Any]) -> tuple[bool, str]:
        return True, ""


ChannelFactory = Callable[[ChannelConfig], Channel]

__all__ = [
    "Channel", "ChannelConfig", "ChannelFactory", "ChannelError",
    "ChannelRegistrationConflict", "ChannelWsClosed", "PermanentChannelError",
    "CancelContext",
    "InboundHandler",
]
