# MySQL 生产升级运维 Runbook：#646/#868 uid 迁移（agent_tasks 两 TEXT 列手工 ALTER）

> 适用：MySQL 8.0（云 RDS）生产库，从「无 uid 列」升级到含 Issue #646/#868 全部 uid 列。
> 基线：docker MySQL 8.0 演练结论（2026-11-03）+ 仓库迁移实现（db_setup.py）。
> 关键事实（e89450769 实测）：MySQL 8.0 对 `TEXT DEFAULT ''` 的 ADD COLUMN **不能**用
> INSTANT/INPLACE——安全门静默跳过导致列未建。本 runbook 覆盖该缺口的手工路径。

## 0. 影响面清单（按锁代价分级）

| 变更 | 表 | 列 | 算法 | 锁窗口 |
|---|---|---|---|---|
| 加列（VARCHAR/INT） | agent_runtimes / runtime_credentials / agent_type_runtime_pool / engine_capabilities / devops_agent_session / chat_agent_session / agent_tasks(engine_uid) / task_messages | VARCHAR(36)/INT | INSTANT | ≈0 |
| 加列（TEXT 带默认值） | **agent_tasks.engine_name_snapshot / agent_tasks.pool_engine_uids** | TEXT | **COPY** | 见 §2 预估 |
| 索引 | 各表 engine_uid 普通索引 | — | INPLACE | 允许 DML |
| FK（RESTRICT） | agent_type_runtime_pool / engine_capabilities / agent_runtimes | — | COPY（小表） | 秒级 |

## 1. 升级前准备（T-1 日）

```sql
-- ① 行数与容量基线（锁窗口预估输入）
SELECT table_name, table_rows, ROUND(data_length/1024/1024,1) AS data_mb
FROM information_schema.tables
WHERE table_schema=DATABASE() AND table_name='agent_tasks';
-- ② 确认当前无残留列（幂等前提）
SELECT column_name FROM information_schema.columns
WHERE table_schema=DATABASE() AND table_name='agent_tasks'
  AND column_name IN ('engine_uid','engine_name_snapshot','pool_engine_uids');
-- ③ 备份（RDS 快照或 mysqldump 单表）
```

## 2. 手工 ALTER（停写窗口内执行）

> 仅 `agent_tasks` 两 TEXT 列需要手工 ALTER；其余列由应用启动迁移（INSTANT）自动完成。
> 建议：先停 worker 入队（应用只读）→ ALTER → 启动应用（迁移自动补索引/回填/盖章）。

```sql
-- 预估锁窗口：COPY 算法≈data_mb × (5~15)s/GB（含 RDS 限速），10 万行/100MB 级 ≈ 10~60s
ALTER TABLE agent_tasks
  ADD COLUMN engine_name_snapshot TEXT NULL,
  ADD COLUMN pool_engine_uids TEXT NULL,
  ALGORITHM=COPY, LOCK=SHARED;
```

- `LOCK=SHARED`：允许并发读、阻塞写（与停写窗口配合，读写均不损）。
- 不带 `DEFAULT ''`（MySQL 8.0 ALTER 路径 TEXT 字面量默认值非法，1064/1845；
  行级代码显式写入，不依赖 DB 默认值——与 `_issue_646_column_ddl` 的翻译一致）。
- 超时兜底：`SET SESSION lock_wait_timeout=5;` 防长阻塞雪崩（对齐 user/db P0 修正）。

## 3. 迁移盖章核验（ALTER 后、放流量前）

```sql
-- ① 两列已建成（含 TEXT 两列）
SELECT column_name, column_type FROM information_schema.columns
WHERE table_schema=DATABASE() AND table_name='agent_tasks'
  AND column_name IN ('engine_uid','engine_name_snapshot','pool_engine_uids');
-- ② 启动应用后：迁移台账已盖章（202611030002~006 全套）
SELECT version, name, applied_at FROM grape_t_schema_migrations
WHERE version IN ('202611030002','202611030003','202611030004',
                  '202611030005','202611030006') ORDER BY version;
-- ③ 回填效果：孤儿 uid 计数（标准环境应为 0）
SELECT COUNT(*) FROM agent_tasks
 WHERE runtime_id>0 AND (engine_uid IS NULL OR engine_uid='')
   AND runtime_id IN (SELECT id FROM agent_runtimes);
```

应用日志核验：`Issue #646 uid 回填完成… 孤儿引用=0` 与
`Issue #868 会话 uid 回填完成… 孤儿引用=0` 各出现一次。

## 4. 回滚预案

| 阶段 | 回滚动作 |
|---|---|
| ALTER 后、应用未启动 | `ALTER TABLE agent_tasks DROP COLUMN engine_name_snapshot, DROP COLUMN pool_engine_uids, ALGORITHM=INPLACE;`（MySQL 8.0 DROP COLUMN 支持 INPLACE，秒级） |
| 应用启动、已回填未放流量 | 同上 DROP（回填数据可重生成，无损）；同步在 `grape_t_schema_migrations` 删除对应版本行，下次启动自动重跑 |
| 已放流量 | **不回滚列**（新写入已依赖 uid 轨道）；仅回滚应用版本，旧应用忽略新列（双读降级保证兼容） |

## 5. 验收清单

- [ ] §2 ALTER 成功，无 1064/1845/timeout 报错
- [ ] §3 ①三列存在 ②迁移台账 5 行盖章 ③孤儿计数=0
- [ ] 应用日志两条回填完成记录（#646/#868）
- [ ] worker claim / chat 会话 / dashboard 引擎显示正常（冒烟）
- [ ] RDS 快照保留 ≥7 天

## 6. 已知缺口（另行跟踪）

- bot_lease `ON DUPLICATE KEY UPDATE` 方言 SQL 缺口（SQLite→MySQL 翻译层不覆盖）：
  不阻塞本次升级，建议单独立 issue。
