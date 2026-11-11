"""im_engine —— 平台无关引擎层（Issue #742 §2.1）。

依赖铁律（docstring 声明即契约）：
  1. 本包只 import im_channel，不 import 任何适配器（适配器启动时注册进 Registry）；
  2. 核心永不读 InboundMessage.raw（哨兵测试 tests/im/test_raw_sentinel.py 守卫）。
"""
from .hashring import build_ring, owner, owners, DEFAULT_VNODES
from .dedup import InboundDedup, InMemoryDedupStore, Verdict
from .lease import LeaseStore, lease_key
from .session import (
    SessionBinding, binding_key, InMemoryBindingStore, RouteDecision, resolve_route,
)
from .router import Router, Installation, Identity, DEBOUNCE_WINDOW
from .replier import Replier
from .supervisor import Supervisor, fingerprint

__all__ = [
    "build_ring", "owner", "owners", "DEFAULT_VNODES",
    "InboundDedup", "InMemoryDedupStore", "Verdict",
    "LeaseStore", "lease_key",
    "SessionBinding", "binding_key", "InMemoryBindingStore", "RouteDecision", "resolve_route",
    "Router", "Installation", "Identity", "DEBOUNCE_WINDOW",
    "Replier", "Supervisor", "fingerprint",
]
