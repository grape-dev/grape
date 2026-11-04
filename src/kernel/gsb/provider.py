"""Grape Service Bus — Provider 工厂（Local/Remote 切换）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.6.

根据 GRAPE_MODE / GRAPE_PROVIDER_MODE 环境变量自动选择 Local 或 Remote Provider：
- single 模式（默认）→ LocalProvider（跨 schema SQL）
- cluster + GRAPE_PROVIDER_MODE=remote → RemoteProvider（HTTP API）
- cluster + 无 GRAPE_PROVIDER_MODE → LocalProvider（共库模式）

新增服务只需 3 步：
1. 定义契约接口（Protocol/ABC）
2. 实现 Local + Remote Provider
3. 调用 register_provider() 注册

使用方式：
    from src.kernel.gsb.provider import get_mgmt_service
    svc = get_mgmt_service()  # 自动选择 Local 或 Remote
    cfg = svc.get_repo_webhook_config("owner/repo")
"""
from __future__ import annotations

import os
from typing import Any, Callable

# Provider 注册表
_PROVIDERS: dict[str, dict[str, Callable[[], Any] | None]] = {}


def register_provider(
    name: str,
    local_cls: Callable[[], Any],
    remote_cls: Callable[[], Any] | None = None,
) -> None:
    """注册服务的 Local/Remote Provider。

    在模块加载时调用（如 src/domains/contracts/__init__.py）。
    新增服务时在自己的模块中调用此函数注册。

    Args:
        name: 服务名（mgmt / runtime / insight / future-service）
        local_cls: LocalProvider 类（无参构造返回实例）
        remote_cls: RemoteProvider 类（无参构造返回实例），可选
    """
    _PROVIDERS[name] = {"local": local_cls, "remote": remote_cls}


def _ensure_registered() -> None:
    """首次调用时惰性注册 contracts Provider（避免模块级 import cycle）。"""
    if not _PROVIDERS:
        from src.domains.contracts import ensure_providers_registered

        ensure_providers_registered()


def get_local_service(name: str) -> Any:
    """强制返回 Local 实现（忽略 GRAPE_PROVIDER_MODE）。

    供 ``/internal/*`` 服务端使用：这些端点的职责就是「被远端调用后在本进程内查数据」，
    如果也跟着 remote 模式走 RemoteProvider，就会 HTTP 回环调用自己
    （mgmt /internal/x → RemoteProvider → mgmt /internal/x …），直接 500/超时。
    """
    _ensure_registered()
    entry = _PROVIDERS.get(name)
    if not entry or entry["local"] is None:
        raise ValueError(f"Local provider not registered for service: {name}")
    return entry["local"]()


def get_service(name: str) -> Any:
    """获取服务实现——根据部署模式自动选择 Local 或 Remote。

    选择逻辑：
    - GRAPE_PROVIDER_MODE=remote → Remote（优先级最高）
    - GRAPE_MODE=cluster 但无 GRAPE_PROVIDER_MODE → Local（共库模式）
    - 其他（single）→ Local

    首次调用时惰性注册 contracts Provider（避免模块级 import cycle）。

    Raises:
        ValueError: 服务未注册
    """
    if not _PROVIDERS:
        from src.domains.contracts import ensure_providers_registered
        ensure_providers_registered()
    if name not in _PROVIDERS:
        raise ValueError(
            f"Unknown service: {name}. "
            f"Did you register_provider('{name}', ...)?"
        )
    entry = _PROVIDERS[name]
    provider_mode = os.environ.get("GRAPE_PROVIDER_MODE", "").strip().lower()
    use_remote = provider_mode == "remote"
    if use_remote and entry["remote"] is not None:
        remote_factory = entry["remote"]
        if remote_factory is not None:
            return remote_factory()
    local_factory = entry["local"]
    if local_factory is not None:
        return local_factory()
    raise ValueError(f"Service '{name}' has no usable provider")


def is_remote_mode() -> bool:
    """当前是否使用 RemoteProvider（仅 GRAPE_PROVIDER_MODE=remote 时为 True）。"""
    return os.environ.get("GRAPE_PROVIDER_MODE", "").strip().lower() == "remote"


# ===== 便捷函数——预注册的三个域 =====


def get_mgmt_service() -> Any:
    """获取 MgmtService 实现。"""
    return get_service("mgmt")


def get_runtime_service() -> Any:
    """获取 RuntimeService 实现。"""
    return get_service("runtime")


def get_insight_service() -> Any:
    """获取 InsightService 实现。"""
    return get_service("insight")
