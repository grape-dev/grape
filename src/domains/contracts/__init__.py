"""contracts/ — 三域服务契约接口（Issue #699 Phase 2 空壳，Phase 5 填充实现）。

Mgmt/Runtime/Insight 三域之间的跨域访问统一收口到本包接口：
- Local provider：同实例跨 schema SQL 实现（Phase 5）
- Remote provider：HTTP API 实现（Phase 5）

分 Schema 下的表名一律经 src.kernel.db.t_mgmt()/t_runtime()/t_insight() 映射。

Phase 5 在此注册三个 Provider 到 GSB Provider 工厂，
业务代码通过 `src.kernel.provider.get_mgmt_service()` 等获取实现。
"""
from __future__ import annotations

from src.kernel.gsb.provider import register_provider

# 注册三域 Provider（Local + Remote）
# Local: 同实例跨 schema SQL
# Remote: HTTP API（集群模式）
from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
from src.domains.contracts.local.insight_local import InsightServiceLocal
from src.domains.contracts.remote.mgmt_remote import MgmtServiceRemote
from src.domains.contracts.remote.runtime_remote import RuntimeServiceRemote
from src.domains.contracts.remote.insight_remote import InsightServiceRemote

register_provider("mgmt", MgmtServiceLocal, MgmtServiceRemote)
register_provider("runtime", RuntimeServiceLocal, RuntimeServiceRemote)
register_provider("insight", InsightServiceLocal, InsightServiceRemote)

# 导出契约接口（供业务代码 import）
from src.domains.contracts.mgmt_service import MgmtService  # noqa: E402
from src.domains.contracts.runtime_service import RuntimeService  # noqa: E402
from src.domains.contracts.insight_service import InsightService  # noqa: E402

__all__ = [
    "MgmtService",
    "RuntimeService",
    "InsightService",
]
