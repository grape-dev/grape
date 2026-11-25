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