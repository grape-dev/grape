"""Registry（Issue #742 §3.4）——Type→Factory 注册表。

规则：last-writer-wins；未注册 Type 静默返回 None（nil 渠道不抛错，
供 Supervisor 跳过未启用平台）。适配器启动时注册进本表，
im_engine 只 import im_channel，通过 Registry 间接拿到适配器实例。
"""
from __future__ import annotations

from typing import Optional

from .channel import Channel, ChannelConfig, ChannelFactory
from .types import Type


class ChannelRegistry:
    def __init__(self) -> None:
        self._factories: dict[Type, ChannelFactory] = {}

    def register(self, t: Type, factory: ChannelFactory) -> None:
        """last-writer-wins：重复注册覆盖旧工厂。"""
        self._factories[t] = factory

    def unregister(self, t: Type) -> None:
        self._factories.pop(t, None)

    def build(self, cfg: ChannelConfig) -> Optional[Channel]:
        """未注册 Type → 静默返回 None。"""
        factory = self._factories.get(cfg.type)
        if factory is None:
            return None
        return factory(cfg)

    def registered(self) -> tuple[Type, ...]:
        return tuple(self._factories)


_default = ChannelRegistry()


def default_registry() -> ChannelRegistry:
    return _default


__all__ = ["ChannelRegistry", "default_registry"]
