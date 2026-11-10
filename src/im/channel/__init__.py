"""im_channel —— 平台无关抽象契约层（Issue #742 §2.1）。

依赖铁律（docstring 声明即契约）：
  1. 本包不 import 任何平台 SDK，不 import im_engine；
  2. im_engine 只 import 本包，不 import 适配器；
  3. 适配器 import 本包，永不 import im_engine 内部；
  4. 核心永不读 InboundMessage.raw（反射哨兵测试守卫，tests/im/test_raw_sentinel.py）。
"""
from .types import ChatType, MsgType, Scope, Type
from .message import (
    InboundMessage, MediaRef, OutboundMessage, ReplyCtx, SendResult, Source,
)
from .capability import Capability
from .channel import (
    CancelContext, Channel, ChannelConfig, ChannelError, ChannelFactory,
    InboundHandler, PermanentChannelError,
)
from .registry import ChannelRegistry, default_registry

__all__ = [
    "ChatType", "MsgType", "Scope", "Type",
    "InboundMessage", "MediaRef", "OutboundMessage", "ReplyCtx", "SendResult", "Source",
    "Capability",
    "CancelContext", "Channel", "ChannelConfig", "ChannelError", "ChannelFactory",
    "InboundHandler", "PermanentChannelError",
    "ChannelRegistry", "default_registry",
]
