# 迁移对齐可行性评估（arch-weekly-11-ljt ← arch-weekly-11）

> 评估基线：本地主分支 `arch-weekly-11-ljt`，已合入 `arch-weekly-11@9421a379d`，`weekly-11@9c4c770b8` 已完全包含（merge 无操作）。
> 数据来源：`src/api/db_setup.py` 注册链（AST 静态解析）、线上三库（`grape_mgmt_db` / `grape_runtime_db` / `grape_insight_db`）的 `grape_t_schema_migrations` 与 `information_schema`、隔离 schema `grape_mig_probe` 干跑。

## 1. 注册链差异

| 来源 | 注册条数 | 说明 |
|---|---|---|
| 本线（arch-weekly-11-ljt） | 19 | 含 4 条 D105 归一 + uuid-pk 补丁 |
| arch-weekly-11（base） | 35 | 含 5 条 2026-11-11 部署补盖/重放迁移 |
| weekly-11 | 18 | 全部已在本线（无独有项） |

- base 独有 17 条（fn 合计约 584 行），本仓库已有其依赖：`_ensure_column`、`_rebuild_table_text_columns`、`src/api/uuid_pk_migration.py`。
- **同号异名 5 组**：base 的 `202611120001-120005`（create-system-credentials-table / rebuild-external-identities-uuid-pk / uuid-pk-grape-accounts / issue-ident-restore-integer-affinity / create-repo-credentials-table）与本线早期改号块 `202611120001-120004` 撞号 → 移植前必须重新分配号段。

## 2. 线上库实况（三库一致）

- 已盖章 32 条；**代码注册但未盖章：0**。
- 曾有一处同号异名：线上 `202611120003 = uuid-pk-grape-accounts-and-user-id-cols`，代码一度把它改成 `repair-autopilot-exec-agent-task-id` → 该修复迁移会被判"已执行"而永久跳过。**已在本次评估中修正**：`repair-*` 迁到 `202611130001`，`uuid-pk` 回归 `202611120003`。

## 3. 真实 schema 缺口（information_schema 实测，三库一致）

| 缺口 | 影响 | base 对应迁移 |
|---|---|---|
| `grape_t_credential_usage_log` 表不存在 | 凭据消费审计静默丢弃（日志已有 1146 告警） | 202611070002 |
| `grape_t_system_credentials` 表不存在 | 系统级凭据无法落库 | base 202611120001（create-system-credentials-table） |
| `agent_tasks.trace_context` 列不存在 | 本线代码已写入该字段（event_pipeline），严格 INSERT 会失败 | 202611100001（本线已注册在 202611120004） |
| `issue_state.scene_user_id` 仍是 `int` | 与 D105 uuid 口径不一致 | 202611091200 |
| 已具备 | `managed_repos.host_user_id varchar(36)`、`unified_user_mapping.user_id varchar(36)` | 202611091400 / 090003 无需再跑 |

## 4. 隔离干跑验证（MySQL 8.0.43，schema `grape_mig_probe`，用本仓 DDL 初始化）

| 迁移 | 第 1 次 | 第 2 次 | 效果实测 |
|---|---|---|---|
| 202611070002 create-credential-usage-log | OK | OK | 表已建（幂等） |
| 202611091200 issue-state.scene_user_id 归一 | OK | OK | `int → varchar(36)` |
| 202611091400 host-user-id 归一 | OK | OK | 幂等无副作用（列已是 varchar） |
| 202611100001 agent_tasks.trace_context | "OK" | "OK" | **列未创建** |

**根因（本次评估最关键发现）**：`_ensure_column(..., "TEXT DEFAULT ''")` 在 MySQL 上
`ALGORITHM=INSTANT` 与 `INPLACE, LOCK=NONE` 均不支持（1845），本仓实现按设计"记 MANUAL REVIEW、不静默 COPY"，
于是**迁移盖了章、列却没建出来**（线上 `202611120004` 已盖章但 `agent_tasks.trace_context` 不存在即为实证）。
另：MySQL 8.0.43 接受 `TEXT DEFAULT ''` 语法但**静默丢弃默认值**（`column_default` 为 NULL）。

反向验证（同一 schema）：
- `_ensure_column(..., "TEXT")` → INSTANT 成功，列已建；
- `_ensure_column(..., "TEXT DEFAULT ''", allow_copy=True)` → COPY 成功，列已建（默认值仍被丢弃）。

## 5. 结论与方案

**可行性**：机械移植可行（依赖齐全），但必须同时解决三类适配，否则会出现"盖章无效"的假绿：

1. **号段**：base 的 `202611120001-120005` 与本线撞号。建议 base 块移植时统一改用 `202611140001+`，不动线上已盖章的 `202611120001-120004`。
2. **TEXT+DEFAULT**：给 `_ensure_column` 增加"TEXT 族 + DEFAULT → 自动降级 `allow_copy=True`"分支（或移植时逐条去 DEFAULT）；否则 MySQL 上继续静默失败。
3. **壳迁移**：base 的 `202611110002`（recreate-tables-skipped-by-version-collision）与 `202611110003`（replay-weekly10-...）是为"保留旧号 + 撞号跳过"策略做的补偿；本线用改号策略已消除撞号前提，**不应移植**，否则重复执行。

**推荐分级实施**：

- **阶段一（低风险，建议尽快）**：只补真实缺口 4 条，号段 `202611140001-0004`：
  1. `create-credential-usage-log-table`
  2. `create-system-credentials-table`
  3. `agent_tasks.trace_context` 补列（用 `TEXT`，或 `allow_copy=True`）
  4. `issue_state.scene_user_id → varchar(36)`（哨兵 0 → ''）
- **阶段二（按需）**：base 其余"归一/回填"迁移（090004/091000/091100/091300/091400/110004），仅对 base 盖章库有额外意义。
- **不移植**：110002 / 110003 两条壳迁移。

**风险与回滚**：数据量小（events 470 / agent_tasks 430 / organization 275 / grape_accounts 66 / managed_repos 19），ALTER 与重建成本可接受；迁移均幂等，回滚靠"迁移前后备份 + 重复执行无副作用"。

**验收口径（阶段一实施时）**：
1. 隔离 schema 干跑两遍，`SHOW COLUMNS` 断言 4 项效果；
2. 启动日志无 `_ensure_column MANUAL REVIEW`；
3. 三库 `information_schema` 断言：两表存在、`agent_tasks.trace_context` 存在、`issue_state.scene_user_id = varchar(36)`；
4. `pytest tests/kernel/test_migration_no_baseline_version_collision.py` 与 `tests/kernel/test_issue_536_migrations.py` 绿；
5. 线上盖章表新增 `202611140001-0004` 四条。