"""VCS provider 端口（kernel 侧）：token 链需要的平台适配器能力面。

为什么有这一层
--------------
`VCSProvider` 家族的**平台 API 行为实现**（建仓 / 协作 / webhook / 命名空间清单）
是外部集成业务，已迁到 `src/domains/mgmt/vcs/providers.py`（见该模块头注释）。
kernel 只保留 token 链**真正用到**的最小方法面：一个 Protocol 加两个工厂函数。
实现由装配层（src/api/contracts_wiring.py）在启动时绑定，依赖方向仍是
kernel <- assembly -> domains，kernel 不 import 任何 domain 代码。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from src.kernel.ports._base import PortNotBound, _bind_registry

_NAME = "vcs_provider"


@runtime_checkable
class VCSProviderPort(Protocol):
    """token 策略链对 provider 的最小依赖（实现方是域侧 VCSProvider）。"""

    def verify_token(self, token: str) -> dict: ...
    def get_user_login(self, token: str) -> str: ...
    def token_expiry(self, token: str) -> dict: ...
    def fetch_user_token(self, username: str, user_id: str) -> str: ...
    def resolve_repo_token(self, repo_key: str) -> str: ...
    def token_storage_field(self) -> str: ...
    def webhook_token_field(self) -> str: ...
    def session_token_field(self) -> str: ...
    def global_bot_meta_key(self) -> str: ...
    def global_bot_status_meta_key(self) -> str: ...
    def pat_create_url(self) -> str: ...


def bind(impl: Any, *, replace: bool = False) -> None:
    """绑定域实现（仅装配层调用）。impl 需提供 create()/create_from_repo_key()。"""
    _bind_registry().bind(_NAME, impl, replace=replace)


def is_bound() -> bool:
    try:
        _bind_registry().get(_NAME)
        return True
    except PortNotBound:
        return False


def create_provider(provider: str) -> VCSProviderPort:
    """与域实现同名：装配层 _Aggregate 按属性名解析，无需适配层。"""
    return _bind_registry().get(_NAME).create_provider(provider)


def create_provider_from_repo_key(repo_key: str) -> VCSProviderPort:
    return _bind_registry().get(_NAME).create_provider_from_repo_key(repo_key)


__all__ = ["VCSProviderPort", "PortNotBound", "bind", "is_bound",
           "create_provider", "create_provider_from_repo_key"]
