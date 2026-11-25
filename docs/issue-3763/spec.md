# Spec: Issue #3763 — autopilot_executions 缺表 1146（runtime 域）

> 来源：Issue #3763 Spec 评论（Issue Agent，developability=ready）与 VOD 代码实查。
> 基线分支：`arch-weekly-11-ljt`。

## 目标

1. `autopilot_executions` 只由 owner 域（runtime / worker 进程）写入；生产 `grape-mgmt`
   不再出现 `(1146) Table '...autopilot_executions' doesn't exist`。
2. 分库环境缺表可被显式发现：建表迁移按 runtime 域幂等补齐；调度器缺表时 fail-fast 告警
   （含表名与期望域），不再每小时静默吞一条无上下文的 ERROR。

## 范围（In）

- `src/api/db_setup.py`：新增 runtime 域 `register_migration`，`CREATE TABLE IF NOT EXISTS`
  + 幂等补列/索引，兼容已盖 legacy-stamp 的存量库。
- `src/domains/runtime/engine/scheduler.py`：表存在性自检（启动 + 每 tick），
  `_create_execution` 缺表 fail-fast 并保留原始异常。
- `tests/`：新增「缺表自检失败」与「三库拓扑执行链路只落 owner 域」两条用例。

## 范围（Out）

- 不重写 autopilot 业务逻辑，不改 `autopilot_tasks`/`autopilot_subscriptions` 归属。
- 不改 `configs/table_ownership.json`。
- 不做生产库手工补表/数据回填（SRE 运维动作）。
- 不重写任务重试与派发策略。

## 现状（代码实查）

- `configs/table_ownership.json`：`autopilot_executions` = runtime，
  `autopilot_tasks` / `autopilot_subscriptions` = mgmt。
- `src/runtime/` 目录已不存在；调度器位于 `src/domains/runtime/engine/scheduler.py`，
  由 worker 启动；mgmt lifespan 不启动 scheduler。
- `db_setup.py` 中该表只出现在 `_NEW_TABLES`（legacy 全量首跑）与 `_ensure_column`
  补列迁移里，**没有负责建表的 `register_migration`**，已盖章存量库不会补建。
- 调度器 `_create_execution` 无表存在性自检，缺表被 `_execute_task` 外层 except 吞掉。

## 验收标准

- [ ] 模拟 runtime 库缺表：启动/首 tick 抛明确错误/告警（含表名与期望域）。
- [ ] 迁移幂等，runtime 库建表成功，重复执行零写入。
- [ ] 三库拓扑下 runtime 写入落 runtime，mgmt 无该表 INSERT/UPDATE。
- [ ] 现有测试 + 新增两条用例通过。
- [ ] 生产 Loki 0 条/小时与执行行推进由 SRE 发布后验收（需先确认生产为拆分后构建）。
