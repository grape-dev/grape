# Issue #3909 任务拆解

| # | 任务 | 文件 | 状态 |
|---|---|---|---|
| 1 | 读路径改域连接 + 降级形态 | `src/domains/runtime/engine/engine_select.py` | 完成 |
| 2 | 契约同步（场景详情） | `src/domains/mgmt/routes/agents.py` | 完成 |
| 3 | 契约同步（巡检列表/详情） | `src/domains/mgmt/routes/autopilot.py` | 完成 |
| 4 | public 包装层透传降级 | `src/domains/runtime/public/__init__.py` | 完成 |
| 5 | 前端降级文案 | `app/src/views/{DashboardView,AutopilotView}.vue` + locales | 完成 |
| 6 | 一键加回改运行时域事务 | `src/domains/runtime/engine/recovery.py` | 完成 |
| 7 | 新增三库拓扑用例 | `tests/runtime/test_issue_3909_detach_hint_topology.py` | 完成 |
| 8 | 既有 uid 用例随契约更新 | `tests/api/test_issue_868_frontend_uid.py` | 完成 |
| 9 | 棘轮 `scan_pgconn --check` + 刷新基线 | `scripts/pgconn_baseline.json` | 完成 |
| 10 | 自审查 + PR + A2A QA | - | 完成 |
