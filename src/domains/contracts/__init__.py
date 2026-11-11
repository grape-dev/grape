"""contracts/ — 三域服务契约接口（Issue #699 Phase 2 空壳，Phase 5 填充实现）。

Mgmt/Runtime/Insight 三域之间的跨域访问统一收口到本包接口：
- Local provider：同实例跨 schema SQL 实现（Phase 5）
- Remote provider：HTTP API 实现（Phase 5）

分 Schema 下的表名一律经 src.kernel.db.t_mgmt()/t_runtime()/t_insight() 映射。

Phase 5 注册三个 Provider 到 GSB Provider 工厂，
业务代码通过 `src.kernel.provider.get_mgmt_service()` 等获取实现。

Provider 注册采用惰性模式（lazy registration）：首次调用 get_service() 时触发，
避免模块级双向导入 contracts.__init__ ↔ contracts.local.* 导致的 import cycle
（check-arch R5 HARD violation）。

契约接口（MgmtService/RuntimeService/InsightService）直接从各自子模块 import：
    from src.domains.contracts.mgmt_service import MgmtService
"""
from __future__ import annotations

__all__ = [
    "ensure_providers_registered",
]

_PROVIDERS_REGISTERED = False


def ensure_providers_registered() -> None:
    """惰性注册三域 Provider（Local + Remote）。

    首次调用时导入实现类并注册到 GSB Provider 工厂；
    后续调用直接返回（幂等）。

    延迟导入避免 contracts/__init__ ↔ contracts.local.* 的模块级环
    （check-arch R5 HARD violation）。
    """
    global _PROVIDERS_REGISTERED
    if _PROVIDERS_REGISTERED:
        return
    _PROVIDERS_REGISTERED = True

    from src.kernel.gsb.provider import register_provider
    from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
    from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
    from src.domains.contracts.local.insight_local import InsightServiceLocal
    from src.domains.contracts.remote.mgmt_remote import MgmtServiceRemote
    from src.domains.contracts.remote.runtime_remote import RuntimeServiceRemote
    from src.domains.contracts.remote.insight_remote import InsightServiceRemote

    register_provider("mgmt", MgmtServiceLocal, MgmtServiceRemote)
    register_provider("runtime", RuntimeServiceLocal, RuntimeServiceRemote)
    register_provider("insight", InsightServiceLocal, InsightServiceRemote)
