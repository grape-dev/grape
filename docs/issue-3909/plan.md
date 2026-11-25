# Issue #3909 方案设计

## 1. 读路径：`engine_select.get_detached_engine_hints`

现状：`with db_lock: c = db.cursor()` 取进程主库游标，查 `scene_engine_detach_log`（owner=runtime）
与 `agent_runtimes`（owner=runtime）→ mgmt 进程恒 1146，异常吞成 `[]`。

改造：

- 取连接改 `db_registry.cursor_for("runtime")`（内核唯一路由；单库/测试拓扑 → `local` 主库）。
  `_engine_uid_by_rid` 复用同一游标（同属 runtime 域）。
- 返回形态由 `list` 改为 `{"hints": [...], "degraded": bool, "reason": "" | "domain" | "query"}`：
  - 正常：`degraded=False`；
  - `DomainUnavailable`（三库缺 DSN，内核明确不回落主库）：`degraded=True, reason="domain"`；
  - 查询异常：`degraded=True, reason="query"`。
- 失败日志统一补连接事实 `route` / `target_db` / `pid` / `host`（复用
  `db_registry.route_of` + `database_of`，只输出库名，不输出 DSN）。

## 2. 契约同步（3 个调用点）

| 调用点 | 原字段 | 新增字段 |
|---|---|---|
| `mgmt/routes/agents.py:get_agent_detail` | `detached_engines` | `detached_engines_degraded`、`detached_engines_unavailable_reason` |
| `mgmt/routes/autopilot.py:list_tasks` | `engine_detached` | `engine_detached_degraded`（按任务仓库分组取降级） |
| `mgmt/routes/autopilot.py:get_task` | `engine_detached` | `engine_detached_degraded` |

前端（`app/src/views/DashboardView.vue`、`AutopilotView.vue`）在降级为真时渲染
「解绑信息暂不可用」，不与「无解绑」同形（locale 新增 `stale_unavailable` /
`engine_detached_unavailable`，en/zh 各一）。

## 3. 同族错库连接：`runtime/engine/recovery.py`

- `recover_engine` 由 `db_lock + session_manager.db` 改 `db_registry.transaction_for("runtime")`
  （三库 → runtime 独立事务；单库 → 主库 + 进程锁，零回归）。
- `DomainUnavailable` → 返回 `{"recovered": False, "reason": "runtime_domain_unavailable",
  "degraded": True}` 并记连接事实日志，不再抛给端点变成无原因 503。
- 其它异常 → `{"recovered": False, "reason": "error: ..."}`（原实现返回 None，调用方
  `out.get` 会 AttributeError）。
- `_readd_scene_bindings` / `_recover_in_tx` / `_reconcile_task_bindings` 的 SQL 全为 runtime
  域表，随事务游标自动落到正确库；`devops_pipeline_scene`（owner=mgmt）经
  `mgmt.public.scene_nodes_row_by_id_any(..., c)` / `scene_nodes_write_by_id(..., c)` 的
  `_same_domain_*` 原语判定为跨域 → 自取 mgmt 连接，不跨库误写。

## 4. 棘轮

`scripts/scan_pgconn.py --check` 在基线分支已因 merge 带入的 `transient_retry.py` 10→12
预存在漂移；本 PR 只收口 `engine_select.py` / `recovery.py`，随后
`--update-baseline` 刷新（PR 说明中显式记录该预存在漂移，不静默吸收）。

## 5. 测试

新增 `tests/runtime/test_issue_3909_detach_hint_topology.py`（参照
`test_3857_engine_onboarding_runtime_domain.py` 的假域游标装配）：

1. 三库：走 runtime 域游标、命中审计行、`degraded=False`；
2. 查询异常：`degraded=True reason=query`，日志含 `route=`/`target_db=runtime`/`pid=`；
3. `DomainUnavailable`：`degraded=True reason=domain`，不回落主库；
4. 单库拓扑：`route=local` 回落进程库，既有行为不变；
5. 静态守门：读点不再 import/使用 `session_manager`，必须出现 `cursor_for("runtime")`；
6. 契约：autopilot 列表/详情降级为真时带 `engine_detached_degraded`；
7. recovery：三库走 `transaction_for("runtime")`；`DomainUnavailable` 返回显式失败原因。
