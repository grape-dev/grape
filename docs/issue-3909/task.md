# Issue #3909 任务拆解

| # | 任务 | 文件 | 状态 |
|---|---|---|---|
| 1 | 读路径改域连接 + 降级形态 | `src/domains/runtime/engine/engine_select.py` | 待办 |
| 2 | 契约同步（场景详情） | `src/domains/mgmt/routes/agents.py` | 待办 |
| 3 | 契约同步（巡检列表/详情） | `src/domains/mgmt/routes/autopilot.py` | 待办 |
| 4 | public 包装层透传降级 | `src/domains/runtime/public/__init__.py` | 待办 |
| 5 | 前端降级文案 | `app/src/views/{DashboardView,AutopilotView}.vue` + locales | 待办 |
| 6 | 一键加回改运行时域事务 | `src/domains/runtime/engine/recovery.py` | 待办 |
| 7 | 新增三库拓扑用例 | `tests/runtime/test_issue_3909_detach_hint_topology.py` | 待办 |
| 8 | 既有 uid 用例随契约更新 | `tests/api/test_issue_868_frontend_uid.py` | 待办 |
| 9 | 棘轮 `scan_pgconn --check` + 刷新基线 | `scripts/pgconn_baseline.json` | 待办 |
| 10 | 自审查 + PR + A2A QA | - | 待办 |