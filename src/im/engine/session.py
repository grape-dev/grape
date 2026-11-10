"""会话绑定（Issue #742 §4.1 步骤⑥ / §5.1）——双轨统一绑定。

chat 轨 repo_im_group_binding + vod 轨 vod_conversations 合并为
grape_t_channel_chat_session_binding 一张表的读写视图。BindingKey 规则见 §5.2：
有话题平台严禁裸 chat_id 当 BindingKey。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..channel.message import InboundMessage, Source
from ..channel.types import Scope


@dataclass
class SessionBinding:
    installation_id: str
    binding_key: str
    chat_type: str = "p2p"
    scope: Scope = Scope.USER
    repo_key: str = ""
    agent_type: str = "chat"
    runtime_session_id: str = ""      # A8：与 web 同源的 opencode 会话
    config: dict = field(default_factory=dict)


def binding_key(source: Source) -> str:
    """BindingKey 组合规则（§5.2）。"""
    if source.thread_root:
        return f"{source.chat_id}#{source.thread_root}"
    return source.chat_id


class BindingStore(Protocol):
    def get(self, installation_id: str, key: str) -> Optional[SessionBinding]: ...
    def upsert(self, binding: SessionBinding) -> SessionBinding: ...


class InMemoryBindingStore:
    """测试替身 + 内存缓存（继承 handler_async.py L113-134 缓存语义）。"""

    def __init__(self, fail_next_get: bool = False) -> None:
        self._rows: dict[tuple, SessionBinding] = {}
        self.fail_next_get = fail_next_get

    def get(self, installation_id: str, key: str) -> Optional[SessionBinding]:
        if self.fail_next_get:
            self.fail_next_get = False
            raise RuntimeError("db locked")
        return self._rows.get((installation_id, key))

    def upsert(self, binding: SessionBinding) -> SessionBinding:
        self._rows[(binding.installation_id, binding.binding_key)] = binding
        return binding


@dataclass
class RouteDecision:
    """⑤b 路由决策（§6）：grape 双轨 scope 决定的 (repo_key, agent_type)。"""

    repo_key: str
    agent_type: str


def resolve_route(installation, binding: Optional[SessionBinding],
                  explicit_agent: str = "") -> RouteDecision:
    """grape 专有路由（§6 代码原样语义）：

    显式命令 > 会话粘性 > repo 轨默认 vod > chat 轨属主兜底。
    """
    repo_key = binding.repo_key if binding else ""
    if explicit_agent:
        return RouteDecision(repo_key, explicit_agent)
    if binding and binding.agent_type:
        return RouteDecision(repo_key, binding.agent_type)
    if installation.scope == Scope.REPO:
        return RouteDecision(installation.repo_key, "vod")
    return RouteDecision("", "chat")


__all__ = [
    "SessionBinding", "binding_key", "BindingStore", "InMemoryBindingStore",
    "RouteDecision", "resolve_route",
]
