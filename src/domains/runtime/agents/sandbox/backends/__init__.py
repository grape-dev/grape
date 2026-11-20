"""backends 包——pkgutil 自动发现平台 + Provider。

新增平台只需：1) 创建 backends/{platform}.py 导出 PROVIDER/ADAPTER
              2) 重启服务
不需要改本文件。
"""
from __future__ import annotations

import functools
import importlib
import logging
import os
import pkgutil
from typing import Callable, Mapping

log = logging.getLogger("sandbox-backends")


@functools.lru_cache(maxsize=1)
def _build_registries() -> tuple[dict, dict, dict]:
    """扫描 backends/ 目录，自动发现所有 Provider + Adapter。

    约定：每个 backend 模块导出 PROVIDER（class）和 ADAPTER（实例）。
    模块名以 _ 或 api 开头的跳过。
    import 失败捕获 (ModuleNotFoundError, ImportError) 并 warn。
    一个模块的 platform 名不匹配或 import 失败不影响其他模块。
    """
    cloud_registry: dict[str, type] = {}
    local_registry: dict[str, type] = {}
    adapter_registry: dict[str, object] = {}

    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name.startswith("_") or mod_info.name == "api":
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{mod_info.name}")
        except (ModuleNotFoundError, ImportError) as e:
            log.warning(f"backend module '{mod_info.name}' import failed: {e}")
            continue

        adapter = getattr(mod, "ADAPTER", None)
        provider = getattr(mod, "PROVIDER", None)
        if not adapter or not provider:
            continue

        if adapter.platform != mod_info.name:
            log.error(
                f"platform mismatch: module={mod_info.name}, "
                f"adapter.platform={adapter.platform}, skipping"
            )
            continue

        if getattr(adapter, "is_cloud", True):
            cloud_registry[adapter.platform] = provider
        else:
            local_registry[adapter.platform] = provider
        adapter_registry[adapter.platform] = adapter

    # [平台开关 2026-11-10 用户裁定] DevBox 暂下架（沙箱凭据表单只留 CubeSandbox）。
    # 在注册表源头过滤——platforms 列表/默认平台/adapter 获取全链统一生效；
    # 重新上架只需改回环境变量默认或从列表移除 devbox，代码零改动。
    disabled = os.environ.get("GRAPESANDBOX_DISABLED_PLATFORMS", "devbox")
    _disabled = {p.strip() for p in disabled.split(",") if p.strip()}
    if _disabled:
        for _p in _disabled:
            if _p in cloud_registry:
                log.info(f"sandbox platform '{_p}' disabled (GRAPESANDBOX_DISABLED_PLATFORMS), hidden from registry")
                cloud_registry.pop(_p, None)
                adapter_registry.pop(_p, None)

    return cloud_registry, local_registry, adapter_registry


def get_cloud_registry() -> dict[str, type]:
    """返回 {platform: ProviderClass}——云沙箱平台。"""
    return _build_registries()[0]


def get_local_registry() -> dict[str, type]:
    """返回 {platform: ProviderClass}——本地沙箱平台（bwrap/null）。"""
    return _build_registries()[1]


def get_adapter(platform: str):
    """返回指定平台的 adapter 实例，无则 None。"""
    return _build_registries()[2].get(platform)


def get_all_adapters() -> list:
    """返回所有 adapter 实例列表。"""
    return list(_build_registries()[2].values())


def get_supported_platforms() -> list[str]:
    """返回所有已注册平台名（含本地）。"""
    return sorted(_build_registries()[2].keys())


def get_cloud_platforms() -> list[str]:
    """返回云沙箱平台名列表（不含 bwrap/null）。"""
    return sorted(_build_registries()[0].keys())


def get_default_cloud_platform() -> str | None:
    """返回优先级最高（priority 最小）的云沙箱平台名。"""
    cloud = _build_registries()[0]
    adapters = _build_registries()[2]
    if not cloud:
        return None
    return min(cloud.keys(), key=lambda p: getattr(adapters[p], "priority", 100))


def default_registry() -> Mapping[str, Callable]:
    """兼容包装——manager.py 调此方法获取 {name: factory} dict。

    用 adapter.build_provider 消除 if/elif 分支。
    所有平台一视同仁——bwrap/null 的"不接受 config"是它们的实现细节。
    factory 返回的 callable 接受全关键字参数。
    """
    _, _, adapter_registry = _build_registries()
    return {platform: functools.partial(adapter.build_provider)
            for platform, adapter in adapter_registry.items()}


def reload_registries() -> None:
    """清除 lru_cache，下次调用 _build_registries() 重新扫描。

    测试 mock adapter 或开发时 --reload 后手动调用。
    进程启动一次构建，正常运行不重试。
    """
    _build_registries.cache_clear()
