"""contracts/ — 三域服务契约接口（Issue #699 Phase 2 空壳，Phase 5 填充实现）。

Mgmt/Runtime/Insight 三域之间的跨域访问统一收口到本包接口：
- Local provider：同实例跨 schema SQL 实现（Phase 5）
- Remote provider：HTTP API 实现（Phase 5+）

分 Schema 下的表名一律经 src.kernel.db.t_mgmt()/t_runtime()/t_insight() 映射。
"""
