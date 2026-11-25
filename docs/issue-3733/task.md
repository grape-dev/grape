# Issue #3733 任务拆解

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-22 | 顺序 = 提交顺序（数据面先行）

| # | 切片 | 文件 | 提交 | 验证 |
|---|---|---|---|---|
| T1 | 红：迁移自检 + 状态列幂等用例 | `tests/kernel/test_issue3733_autopilot_dispatch_state_migration.py` | `test(#3733): 不可派发状态列/自检迁移用例（红）` | 用例失败（列不存在） |
| T2 | 绿：补列迁移 + 存量幂等自检 | `src/domains/mgmt/migrations/autopilot_dispatch_state.py`、`src/api/db_setup.py`（接线）、`src/domains/mgmt/schema_ddl.py` | `feat(#3733): autopilot_tasks 不可派发状态列与一次性自检` | T1 转绿 |
| T3 | 红：多 tick 单条留痕 + 库内状态 + 手动触发零副作用 | `tests/runtime/test_autopilot_empty_instruction_guard.py` | `test(#3733): 跳过/触发拒绝留痕与状态用例（红）` | 用例失败 |
| T4 | 绿：mgmt 列白名单 / 可移植查询 / 状态写原语 | `src/domains/mgmt/public/__init__.py` | `feat(#3733): mgmt 域暴露不可派发状态读写原语` | T3 前半转绿 |
| T5 | 绿：调度器跳过与手动触发改造 | `src/domains/runtime/engine/scheduler.py` | `feat(#3733): 空指令任务状态化 + 状态变更单次留痕` | T3 全绿 |
| T6 | 绿：接口出参与保存恢复 | `src/domains/mgmt/routes/autopilot.py` | `feat(#3733): 任务出参暴露不可派发原因，补齐指令清状态` | `_row_to_task` 用例 |
| T7 | 界面：列表徽标 + 详情提示条 + 词条 | `src/api/frontend/app/src/views/AutopilotView.vue`、`shell/locales/{zh,en}/autopilot.json` | `feat(#3733): 任务页展示不可派发原因与补齐入口` | 前端语法校验 + 词条对齐 |
| T8 | 回归 | `tests/runtime/`、`tests/kernel/` 相关文件 | — | `pytest` 通过 |

关联仓库 `gitcode:grape-dev/grape-admin`：核查后无 Autopilot 代码（无 autopilot 引用），本次不改动，PR 评论说明。

## PR #2157 门禁复审整改提交登记

> 本节登记**审查整改轮**的提交（不属于 T1~T8 原始切片的增量整改），便于逐条回溯到复审结论。

| 复审项 | 提交 | 改动 | 验证 |
|---|---|---|---|
| BLOCKED（迁移三态判据） | `b8183a8c1` | `migrations/autopilot_dispatch_state.py`：`classify_column_default` 四态化（ok/missing/null/other），ensure 与 post_check 共用同一判据 | `tests/mgmt/test_issue_4262_autopilot_state_defaults_migration.py` |
| Critical（建单 INSERT） | `c5593a941` | `routes/autopilot.py` / `org_analysis.py`：建单与组织分析 INSERT 显式写三列为空串 | 路由用例（真实落库三列为 `''`） |
| Critical（列 DDL） | `5f524d107` | 建单 DDL 改 `VARCHAR(255) NOT NULL DEFAULT ''`（TEXT 在 MySQL 8.0.46 上落地成 `DEFAULT NULL`） | DDL 断言 + `_ensure_column` 用例 |
| Required（reason 断言） | `fb7638995` | 用例对齐稳定 reason code（`blocked_empty_instruction`） | `tests/mgmt/test_issue3733_autopilot_dispatch_state_route.py` |
| Critical（迁移撞号） | 本轮 | 四条迁移共用 `202611250001` → 重新编号：`001`=events-cascade、`002`=卡片 payload、`003`=events payload mediumtext、`004`=payload-guard 建表（并补 post_check）、`005`=repo-vcs-health、`006`=grape_t_obs_webhook_rejects 时间窗索引 | `tests/api/test_migration_track_guard.py`（静态 + 运行时双守卫） |
| Required（已盖章库漂移） | 本轮 | `db_setup.verify_migration_targets` mgmt 分支加 `state_column_defaults_converged`，旧判据盖章的库启动即暴露漂移 | `test_verify_migration_targets_exposes_state_default_drift` |
| Optional（1406 预检） | 本轮 | `_precheck_state_column_lengths`：MODIFY 前统一量存量长度，超长即报「列/行数/最长」且零 DDL | `test_ensure_reports_overlong_values_before_any_ddl` |
| Optional（老轨索引） | 本轮 | `idx_grape_t_obs_webhook_rejects_time_repo` 从冻结老轨 `_create_indexes()` 改为注册式迁移 `202611250006`（存量库才会执行） | `test_legacy_index_ddl_is_frozen` 转绿 |
