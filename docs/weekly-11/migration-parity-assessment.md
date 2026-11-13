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

---

# 阶段一实施结果（2026-11-12）

已按上文"阶段一"落地并验证，号段 `202611140001-0004`：

| 版本 | 名称 | 落地内容 |
|---|---|---|
| 202611140001 | create-credential-usage-log-table | 建 `grape_t_credential_usage_log`（9 列） |
| 202611140002 | create-system-credentials-table | 建 `grape_t_system_credentials`（7 列 + UNIQUE） |
| 202611140003 | repair-agent-tasks-trace-context | 给 `agent_tasks` 补 `trace_context TEXT`（无 DEFAULT + allow_copy + strict） |
| 202611140004 | issue-state-scene-user-id-text-normalize | `issue_state.scene_user_id` INT → VARCHAR(36)，哨兵 0 → ''；顺带放宽 `unified_user_mapping.dingtalk_user_id` 到 VARCHAR(191) |

## 附带修复

1. **`_ensure_column` 新增 `_strip_text_default`**：TEXT 族带任意 DEFAULT 时先剥离再 INSTANT/INPLACE
   重试，失败才按 `allow_copy` 退 COPY。彻底消除"迁移盖章、列未建"的假绿（202611120004 的病根）。
2. **既有 202611120004 的 fn** 改为 `TEXT + allow_copy + strict`（新装库直接生效）。
3. **新增 `scripts/sync_domain_views.py`**（幂等 `CREATE OR REPLACE VIEW`）：
   **关键新发现**——跨库可更新视图是**建视图时的列快照**，基表加列/新表都不会自动出现在视图里：
   * `grape_mgmt_db` / `grape_insight_db` 的 `agent_tasks` 视图缺 `trace_context`（48 → 49 列已重建）；
   * 两张新凭据表在 mgmt/insight 需要视图转发（runtime 为 owner 基表）。
   本次同步 6 个视图；里程碑内任何"基表加列/建表"都必须跟跑此脚本。
4. **140003 增加视图跳过分支**：非 owner 库的 `agent_tasks` 是视图时直接 return（列归属 owner 基表），
   避免每次启动刷 `MANUAL REVIEW`/1347。

## 实测证据（两机）

- 三库盖章：`202611140001-0004` 全部存在（`grape_t_schema_migrations` 为 mgmt 基表，runtime/insight 为视图共享）。
- 对象断言：`grape_t_credential_usage_log` / `grape_t_system_credentials` 在 `grape_runtime_db` 为基表，
  mgmt/insight 为视图；`agent_tasks.trace_context` 三库均可见（49 列）；`issue_state.scene_user_id = varchar(36)` 三库一致。
- 业务实证：`credential_usage_log.log_usage(...)` → 经 mgmt 视图写入 → `list_by_credential` 读回 1 行（清理后为 0），
  说明审计链路不再静默丢弃。
- 启动日志：两机四进程重启后 `MANUAL REVIEW` 0 条、`usage_log ... 1146` 0 条（此前每轮都有）。
- 回归：`tests/kernel` 600 passed / 51 failed（实施前 594/54）；`tests/api` 429 passed / 54 failed；
  `tests/arch` 22 passed / 1 failed（silent-except ratchet 基线存量）；`check-arch` PASS；`make lint` import OK；
  新增守卫 `tests/kernel/test_migration_parity_stage1.py` 3 passed。
- 隔离干跑（`grape_mig_probe4`）：四条迁移各跑两遍全部 OK，效果与上面一致，`_DDL_MANUAL_REVIEW` 无与四条相关的条目。
- 备份：三库 `mysqldump --single-transaction` 产物留档于 `<backup-dir>/{grape_mgmt_db,grape_runtime_db,grape_insight_db}-<timestamp>.sql.gz`。

## 阶段二（未实施，保持评估结论）

base 其余归一/回填迁移（090004/091000/091100/091300/091400/110004）按需移植；
两条 replay 壳（110002/110003）不移植。移植时统一改用 `202611150001+` 号段。

> 运维要点（视图快照、共享盖章视图、.gitignore 吞脚本、新增迁移检查清单）见
> `docs/weekly-11/domain-view-and-ignore-pitfalls.md`。
