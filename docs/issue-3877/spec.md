# Issue #3877 需求文档：runtime 库 events 级联补偿五列补齐 + 写侧降级

## 目标

三库（mgmt / runtime / insight）拓扑下，**存量** runtime 库的 `events` 表永久缺
`cascade_reason` / `report_missing` / `triggered_by` / `cascade_task_status` /
`cascade_prev_event_status` 五列，导致 `_cascade_event_terminal()` 的级联终态收敛
UPDATE 每次抛 `1054 Unknown column 'cascade_reason' in 'field list'`：

- 事件终态无法收敛（任务已终态、事件永停 running/pending）；
- 同 try 内的 `event_active_guard` 释放被跳过，只能等 TTL 30min 兜底 → 同角色新交接被去重挡死；
- 外层 `except` 只打一条 WARNING 并 `return False`，无指标无告警（72 小时 54 次无人知晓）。

## 现状（源码定位）

| 位置 | 现状 |
| --- | --- |
| `src/domains/runtime/engine/service.py::_cascade_event_terminal_impl` | UPDATE 一次性写 5 个 cascade 列 + 5 个终态列，缺列即整条失败 |
| 同上，guard 释放 | 在 UPDATE **之后**同一个 `try` 内，UPDATE 抛错即不执行 |
| 同上，失败处理 | `log.warning(...)` + `return False`，无指标 |
| `src/domains/runtime/schema_ddl.py` `CREATE TABLE events` | 五列已在（新装库 OK） |
| `src/domains/runtime/migrations_ddl.py::ALTERS_SESSION_EVENT_COLUMNS` | 五列已在（**只被冻结的 legacy 轨道 `_migrate_tables()` 消费**，已盖 legacy-stamp 的存量库启动整段不跑） |
| `src/api/db_setup.py` 登记表 | 无任何 `domains=("runtime",)` 的迁移负责 events 的 cascade 五列 |

根因链：`8377ac22a8` 把五列加进了 runtime `schema_ddl`（新库）与
`ALTERS_SESSION_EVENT_COLUMNS`（冻结老轨），存量库两条路径都不生效 → 现网 runtime 库
至今缺列 → 1054 照旧。

## 期望行为

1. **登记式补列**：新增 `src/domains/runtime/migrations/events_cascade_columns.py`，
   幂等 `_ensure_column` 五列 + `post_check` 五列在位才盖章；在 `src/api/db_setup.py`
   用**字面量** `register_migration(version="202611250001", name="events-cascade-compensation-columns",
   kind="safe", domains=("runtime",), ...)` 登记。
2. **缺列降级**：写前取 `events` 实际列集，只写存在的列；缺 cascade 列时终态字段
   （status/exit_code/completed_at/result/structured_output）照常收敛，不再整条失败。
3. **guard 必释放**：`event_active_guard` 释放移入 `finally`，终态 UPDATE 失败也删除并留痕。
4. **可观测**：缺列降级 / 收敛失败改为结构化日志（Loki 可检索）+ Prometheus 计数指标。
5. **存量补偿**：对「任务已终态、事件仍活跃」的 seq 提供幂等批量重跑入口（走 `_real` 回填路径）。

## 验收标准

- [ ] 三库拓扑回归：跑一次任务终态 cascade 收敛，runtime 库 `events` 五列写入成功，
      且 `event_active_guard` 对应行已删除。
- [ ] 存量库路径回归：模拟「已盖 legacy-stamp 台账 + 缺五列」的库启动，
      `202611250001` 被盖章且五列在位。
- [ ] 缺列场景下 UPDATE 降级为「只写存在列」，无 1054，guard 仍释放。
- [ ] `tests/api/test_migration_track_guard.py` / 域归属守卫全绿，版本号唯一、`domains=("runtime",)`。
- [ ] 上线后 Loki `{service_name=~"grape-(worker|mgmt)"} |= "_cascade_event_terminal: failed"` 归零。