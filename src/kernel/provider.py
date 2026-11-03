"""域间通信 Provider 工厂——根据部署模式自动选择实现（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.1.

兼容入口：委托 src.kernel.gsb.provider 的便捷函数。
业务代码通过本模块或 gsb.provider 均可获取服务实现。

使用方式：
    from src.kernel.provider import get_mgmt_service
    svc = get_mgmt_service()  # 自动选择 Local 或 Remote
    cfg = svc.get_repo_webhook_config("owner/repo")
"""
from __future__ import annotations

from src.kernel.gsb.provider import (
    get_mgmt_service,
    get_runtime_service,
    get_insight_service,
    get_service,
    register_provider,
    is_remote_mode,
)

__all__ = [
    "get_mgmt_service",
    "get_runtime_service",
    "get_insight_service",
    "get_service",
    "register_provider",
    "is_remote_mode",
]
