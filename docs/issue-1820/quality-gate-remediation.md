# PR #1820 架构守护复审整改记录（2026-11-19）

> 对应 grape_bot「架构守护 3.0 审查报告（PR #1820）」的 BLOCKED / Critical / Medium / Informational 发现。
> 每条给出整改点、证据位置与验证方式。

## BLOCKED

| 发现 | 整改 | 证据 | 验证 |
|---|---|---|---|
| **G-1** 迁移版本守卫新增红 | 按改号事实刷新 `tests/arch/migration_versions_baseline.json`（旧号移除、新号 `202611182014/15/4000` 与本次新增迁移录入） | `tests/arch/migration_versions_baseline.json`；`scripts/gen-migration-baseline.py --write` | `pytest tests/arch/test_migration_version_integrity.py` 绿 |
| **A12** 新表主键用自增 id | 6 张组织新表主键改为 `TEXT PRIMARY KEY`（32 位 uuid，Python 侧生成）；引用列 `dispatch_id` / `issue.org_context_snapshot_id` 同步字符串化 | `src/domains/mgmt/schema_ddl.py:816-930`；`src/domains/runtime/schema_ddl.py:241`；`src/api/db_setup.py` 迁移 `202611191000` | 组织相关 mgmt/runtime 测试全绿；新表 INSERT 均显式生成 id |
| **A10** 运行态直调管理态内部模块 | runtime 改用 `mgmt.public`：token（github/gitcode）、组织快照读取、组织下派快照/终态集合；Issue URL 复用本域 `_vcs_issue_url` | `src/domains/runtime/issues_center.py:844`；`src/domains/runtime/engine/service.py:1092,3474` | `check-arch.py` R4 195 → 186；无 `runtime -> mgmt.<internal>` 新增 import |
| **A11** 跨域数据获取未走内部 api | 为 runtime 增加 public 写口（`cancel_issue_core`/`cancel_task`/`cancel_direct_task`/`run_pipeline`/`run_git`/`clone_auth_scheme_for`/`storage_save_bytes`/`storage_delete_object`），为 mgmt 增加 `load_org_snapshot_files` 等；mgmt 编排全部改走 `runtime.public` | `src/domains/runtime/public/__init__.py` 尾部；`src/domains/mgmt/public/__init__.py` 尾部；`org_dispatch.py`、`org_knowledge.py`、`org_context_repo.py`、`org_provision.py`、`org_repo_map.py`、`routes/orgs.py`、`pipeline_config.py` | `check-arch.py` R4 下降 9 条；相关 mgmt/runtime 测试绿 |
| **A13** 新特性缺可观测接入 | 新增 `src/kernel/org_metrics.py` 可安装收集器（dispatch/provision 计数、时延、在途 gauge），由 insight metrics server `install(registry)`；`src/kernel/otel.feature_span` 接入 `org.provision`、`org.dispatch.create_child`；Logs 沿用既有 Loki | `src/kernel/org_metrics.py`；`src/domains/insight/observability/__init__.py:98-101`；`src/domains/mgmt/org_provision.py:582`；`src/domains/mgmt/org_dispatch.py:544` | 指标注册实测；`docs/org-pipeline/org-runtime-budget.md §5` |
| **D1** Vue 硬编码中文 | 组织能力卡片 9 处文案 key 化（`organizations.workbench.map_summary.*`），zh/en 双语补齐 | `src/api/frontend/app/src/views/Organizations.vue:647-699`；`shell/locales/{zh,en}/organizations.json` | `scripts/check-i18n-keys.py` 绿（374=374）；i18n 缓存棘轮绿 |

## Critical

| 发现 | 整改 | 证据 |
|---|---|---|
| **A6** 新表无 `grape_t_` 前缀 | 6 表统一改名 `grape_t_org_*`；新增迁移 `202611191000` 对存量库执行 `ALTER TABLE ... RENAME` + 主键重建，不只改 DDL | `schema_ddl.py`、`configs/table_ownership.json`、`db_setup.py` |
| **B2** 无来源数值 | 为 4 组数值补 `rationale` 并登记到 `docs/org-pipeline/org-runtime-budget.md`（含待决策节）；绑定租约 30s 与内核默认 60s 的差异写明依据 | `org_provision.py:46`、`user/org_db.py:21-24`、`org_context_store.py:29`、`kernel/context_digest.py:22-26` | `magic_number_audit` 对上述文件 unbacked=0 |

## Medium / Informational

| 发现 | 整改 |
|---|---|
| **A7** 下派/快照无 TTL | 新增 `src/domains/mgmt/org_retention.py`：终态批次 90 天 TTL，按聚合→目标→快照→批次顺序清理并删磁盘目录；由 sweeper 每日节流触发；非终态保留。测试 `tests/mgmt/test_org_dispatch_retention.py` |
| **C3** 供给链路无审计 | 新增 `org_provision_started/completed/failed` 审计事件并接入 `provision_org`；`org_provision_` 前缀别名从空集改为实际事件集 |

## 复跑建议

```bash
python3 scripts/check-arch.py
pytest tests/arch tests/mgmt tests/runtime/test_issue_2118_org_pipeline_flow.py -q
scripts/check-i18n-keys.py
python3 .grape/skills/qa/production-architecture-guardian/scripts/magic_number_audit.py . --json
node tests/js/test_org_pipeline_page.js
```
