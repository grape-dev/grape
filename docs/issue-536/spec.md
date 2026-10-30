# Spec: Issue #536 — 数据库迁移版本化与 DDL 安全带（蓝绿分流第 1 层地基）

> 来源：Issue Agent Spec（已发布于 Issue 评论）+ 设计文档 docs/weekly-09/migration-versioning-and-ddl-safety-design.md（分支 docs/weekly-09-migration-design）+ Coding Agent 代码考察确认 | 2026-10-30

## 背景与根因

- grape-main 启动时全量重跑 `_run_all_migrations()`（src/api/db_setup.py:2555，5047 行、133 处 ALTER），每次启动重放全部幂等检查；
- 迁移中混有 MODIFY / RENAME / DROP COLUMN 及全表回填（task_messages 约 400 万行），在线实例存量连接持有 MDL 时 ALTER 排队无限等待；
- 蓝绿分流下该阻塞源会扩大为整个绿集群的阻塞，因此迁移向后兼容是蓝绿前置条件。

## 需求（用户可见行为）

1. **迁移只跑一遍**：`schema_migrations` 版本表 + Migration 注册表执行器；在线库首启全量跑+盖章，第二次启动 0 pending，启动时间明显缩短；
2. **kind 分流**：safe（直接执行）/ backfill（分批 LIMIT 1000+每批独立事务，失败跳过不阻断启动）/ cleanup（破坏性 DDL，受 meta 表 `min_code_version` 门槛）/ legacy-stamp（存量 133 处历史迁移首跑全量后一次性盖帽收敛）；
3. **DDL 安全带**：`_ensure_column` MySQL 侧强制 `ALGORITHM=INSTANT` → 降级重试 `ALGORITHM=INPLACE, LOCK=NONE`，再失败记 ERROR 人工清单，绝不静默 COPY；SQLite 路径不变；
4. **危险 DDL 唯一入口**：`_destructive_ddl()` 收敛 DROP/RENAME/MODIFY COLUMN 调用点 + meta 表 `min_code_version` 门槛 + CI/pre-commit grep 卡点；
5. **会话级锁超时**：迁移专用连接 `SET SESSION lock_wait_timeout=10`，超时报错跳过该条迁移并附 MDL 持有者 processlist 摘要；
6. checksum 不匹配只告警不阻断（fail-safe 不 fail-stop）；
7. 全新安装路径与在线库路径 schema 结果一致。

## 范围外（Out of Scope）

- 133 处历史迁移函数体一行不动（打包进 legacy-stamp）；
- 蓝绿分流方案本身（另单）；
- session_manager / mysql_compat 改动；
- SQLite 路径行为变化（ALGORITHM 仅 MySQL 后端下发）。

## 验收标准

1. 在线库升级后首启全量跑+盖章；二次启动 0 pending、启动时间明显缩短；
2. 全新空库 init 后与在线库 information_schema 列级 diff = 空；
3. 演练库 400 万行表+长事务：加列 INSTANT 秒过或 10s 报错跳过（含 MDL 持有者摘要），不无限等待；
4. 测试库模拟蓝绿 v1 活跃+v2 跑 safe 迁移：v1 无阻塞、v2 成功；
5. 所有现有测试通过；新增测试覆盖版本化/安全带/门槛逻辑。
