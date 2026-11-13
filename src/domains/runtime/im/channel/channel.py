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
    "PermanentChannelError", "CancelContext", "InboundHandler",
]
