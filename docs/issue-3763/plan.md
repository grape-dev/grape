# 方案设计：Issue #3763 — autopilot_executions 缺表 1146

## 分层与数据流

```
worker/event（runtime 域）
    scheduler.scheduler_loop
        _assert_autopilot_executions_table()   ← 启动 + 每 tick 表存在性自检
        _execute_task → _create_execution      ← 缺表 fail-fast（AutopilotSchemaError）
            INSERT INTO autopilot_executions   ← runtime 域连接

mgmt 进程
    routes/autopilot.py → runtime.public 原语   ← 跨域读/删/写 findings，不直写该表

迁移（event 进程 GRAPE_MIGRATION_OWNER=runtime）
    register_migration(domains=("runtime",))
        CREATE TABLE IF NOT EXISTS autopilot_executions + 幂等补列/索引
```

## 关键决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 缺表处理 | 静默重试 / fail-fast 告警 | fail-fast + 明确告警 | 现场已静默失效 3 天以上 |
| 建表归属 | mgmt 兜底建表 / runtime 迁移建表 | runtime 迁移 | 与 table_ownership 一致，避免 mgmt 写该表 |
| 自愈方式 | 运行时手写 DDL / register_migration | register_migration | 符合现有迁移规范，可被域守卫执行 |
| 连接 | 进程库 / owner 域连接 | `_migration_conn("runtime")` | 三库拓扑下 mgmt 进程库无该表 |

## 实现要点

1. **迁移**：`db_setup.py` 新增 `_fn_create_autopilot_executions_table`，复用
   `_AUTOPILOT_EXECUTIONS_DDL`（`CREATE TABLE IF NOT EXISTS`）；建表后用
   `_ensure_column` 幂等补齐 `agent_type`/`findings_json`/`agent_task_id`/
   `params_json`/`notified`；`CREATE INDEX IF NOT EXISTS` 补齐 task_id/status/
   agent_task_id 索引；`domains=("runtime",)`，版本号取目标分支未占用的 `202611230006`
   （R2 换号：`202611230003` 已被基线 #3800 `autopilot-task-failure-state-columns` 占用，
   撞号守卫只 log.error 不抛、runner 按 version 判已执行会永久跳过本建表迁移；
   `202611230004` 已用、`202611230005` 已被 #1981/#3794 预定）。
2. **自检**：`scheduler.py` 新增 `AutopilotSchemaError`、`_autopilot_executions_table_present`
   （SQLite `sqlite_master` / MySQL `information_schema`）与
   `_assert_autopilot_executions_table`（缺失抛含表名 `autopilot_executions` 与
   期望域 `runtime` 的异常）。`start_scheduler()` 先自检并记录明确告警；
   `scheduler_loop()` 每 tick 自检，缺表跳过本轮并 ERROR 告警。
3. **fail-fast**：`_create_execution` 插入前自检；INSERT 若命中「表不存在」错误
   （1146 / doesn't exist / no such table）则包装为 `AutopilotSchemaError` 并 `from` 保留原始异常。
   `_execute_task` 单独捕获该异常，记录含表名/域的 ERROR，不再被普通 except 吞成无上下文日志。

## 测试 seam

- 公开 seam：`scheduler._assert_autopilot_executions_table`（自检契约）与
  `scheduler._create_execution`（写入口 fail-fast）。
- 拓扑 seam：`configs/table_ownership.json` + 迁移 `domains` + runtime.public
  `exec_any` 的域路由（`db_registry.cursor`）。

## 风险与对策

| Risk | Impact | Mitigation |
|------|--------|------------|
| 生产仍跑拆分前构建 | High | SRE 先核验构建/进程装配，发布后以 Loki/DB 复核 |
| 1146 特判掩盖其它 DB 错误 | Med | 仅匹配表不存在特征，保留原始异常并告警 |
| mgmt 历史库存在同名表 | Low | 迁移只登记 runtime 域，CREATE IF NOT EXISTS |
| 启动期自检影响可用性 | Med | 只记录告警并跳过 autopilot tick，不影响 worker 其它任务 |
