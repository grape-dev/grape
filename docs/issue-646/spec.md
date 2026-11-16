# Spec: Issue #646 — 引擎/worker 表 uid 稳定键改造（开发版）

> 本文件为开发侧落地摘要；完整需求与设计以 Issue #646 评论中的 **Spec v3.1**（Issue Agent 发布，评级 ready）为准。
> 实现基线分支：**weekly-09**（Spec v3.1 修订：不复用/不合并旧 PR #813，其 base=weekly-09 旧个人分支（已归档）仅作历史参考；本 PR 在新基线重新落地，代码等价重放 + 基线适配）。

## Objective
消除引擎/worker 表自增 id 主键 + 跨表裸引用风险（悬挂引用 / id 漂移 / 无 FK / 可枚举）：
为 `agent_runtimes` 引入 `engine_uid`（UUIDv5 自然键派生，幂等防漂移），为 `runtime_credentials`
引入 `credential_uid`（UUIDv4，凭据名可变），引用方双读切换（uid 优先、旧 id 兜底），MySQL 补真实 FK，
经 `grape_t_schema_migrations` 幂等迁移。

## Key Decisions（Spec v3.1 摘要）
| 项 | 决策 |
|---|---|
| engine_uid | `uuid5(固定 namespace, credential_uid|name|scope|owner_id|host_name)`，服务端注册/心跳时派生 |
| credential_uid | UUIDv4，创建时一次性生成，终身不变 |
| 存储 | SQLite TEXT / MySQL CHAR(36) 语义（TEXT 列），小写连字符规范形 |
| 自增 id | 保留兼容（双读期） |
| agent_tasks | `engine_uid` + `engine_name_snapshot`（引擎删除后历史可读）；`pool_engine_ids` 旁新增 `pool_engine_uids` |
| MySQL FK | 池/能力表 engine_uid → agent_runtimes(engine_uid)、agent_runtimes.credential_uid → runtime_credentials(credential_uid) 均 **ON DELETE RESTRICT**（复审裁定：静默级联蒸发比可见悬挂更危险）；删除一律走显式清理链（统一例程 + 对账报告），RESTRICT 兜底防漏 |
| 删除路径 | 三条删除路径（去重/吊销/devstage，另覆盖 scope 切换重复行、刷新过期行）统一接入 `_rebind_runtime_references` / `explicit_cleanup_runtime_references`：改绑或显式清理池/能力/会话/绑定/场景引用并出对账报告；agent_tasks 按快照语义保留 uid+名称（恢复走 redispatch 重绑） |
| 孤儿引用 | 回填置 NULL + 迁移报告（log + 进程内报告），不静默丢弃 |
| 前端 | 本仓库无前端代码（无 .vue/web 目录），展示与绑定入口改造不在本仓范围 |

## In Scope（本 PR）
- DDL：六表加 uid 列 + UNIQUE 索引；全新库直接建出
- 迁移：`202611030002`（safe 加列+索引）、`202611030003`（backfill 回填+报告）、`202611030004`（safe MySQL FK）（#826 arch 基线重编号：202611020001/2/3 已被 patrol-findings/issue627 迁移占用并盖章）
- `save_agent_runtimes` / `runtime_register`：按自然键派生 uid，按 uid upsert
- 任务写路径：agent_tasks 插入时双写 engine_uid/engine_name_snapshot/pool_engine_uids
- 场景绑定：服务端支持 uid 输入并校验（scenes 校验 + agents 绑定解析）
- 双读 helper：uid↔id 互解析（`resolve_engine_refs` 等）

## Out of Scope
- 前端页面改造（无前端代码）；旧 id 读路径移除（双读稳定后的后续版本）；历史 task_messages 全量回填（快照语义）
- 会话表（devops_agent_session / chat_agent_session）runtime_id 双读：**阶段 4**（task.md 已排期明确提交节点：PR 合入后的下一个功能提交）
- 观测/日志类 runtime_id 引用（runtime_health_scores / obs_runtime_samples / obs_log）：可再生数据与 append-only 日志，孤儿随采样淘汰（裁定记录，见 Issue #646 评论）
- org_id 不入 engine_uid 派生源（派生源 = 现有 UNIQUE 键逐列镜像，org 语义随 credential_uid 走；防止后续误加列，裁定记录）

## 运维备注（QA 复审 R1，merge 前补）
- **生产 MySQL 升级加 TEXT 列需 ALGORITHM=COPY**：`agent_tasks.engine_name_snapshot` / `agent_tasks.pool_engine_uids`
  为 TEXT 列，MySQL 8.0 ALTER ADD TEXT 列不支持 INSTANT/INPLACE（错误 1845），安全带不静默 COPY——
  生产 MySQL 上若 INSTANT/INPLACE 不可用，需 DBA 低峰手工执行：
  `ALTER TABLE agent_tasks ADD COLUMN engine_name_snapshot TEXT NULL, ALGORITHM=COPY;`
  （`pool_engine_uids` 同理）。**agent_tasks 是最热写入表，COPY 重建锁表，务必低峰执行**。
  列未建成时迁移 `202611030002` 不盖章，下次启动自动重试（无中间态）。
- MySQL 侧两列 DDL 为 `TEXT NULL`（无默认值；MySQL 禁止 BLOB/TEXT 默认字面量，ALTER 路径不经
  mysql_compat CREATE TABLE 翻译层），SQLite 侧保持 `TEXT DEFAULT ''`；行级代码全部显式写入，不依赖 DB 默认值。

## 验收备注（QA 复审 R4）
- `agent_type_runtime_pool` 现行为已弃用表（tests/test_runtime_pool.py 文件头自述）；本 PR 对其的
  迁移改造（加列/回填/FK/改绑）按 **schema 正确性** 验收，不按业务行为验收。
