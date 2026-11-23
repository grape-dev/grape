# Task: Issue #4066 任务拆解

> 一竖切片一测试一提交（TDD 红/绿分提交）。基线 `arch-weekly-11-ljt`，开发分支 `feat/issue-4066`。

| # | 切片 | 红（测试提交） | 绿（实现提交） | 验收 |
|---|------|----------------|----------------|------|
| T1 | 保留语义收窄（白名单） | `tests/kernel/test_issue_4066_platform_managed_env.py` | `src/kernel/scene_custom_env.py` | 白名单三项非保留；其余 `GRAPE_*`/`BOT_*`/`USER_*` 仍保留 |
| T2 | autopilot 写侧校验 | `tests/mgmt/test_issue_4066_autopilot_custom_fields.py` | `src/domains/mgmt/routes/autopilot.py` | create/update 保留名 400 + 冲突名回显 + 不落库；白名单可保存 |
| T3 | 注入日志收敛 | `tests/runtime/test_issue_4066_autopilot_env_inject_log.py` | `src/domains/runtime/engine/scheduler.py` | 单条 info 汇总；白名单注入生效；敏感名跳过无逐条 WARNING |
| T4 | 场景注入平台优先 | 并入 T3 测试（event_pipeline 用例） | `src/domains/runtime/pipeline/event_pipeline.py` | 平台已注入的 `GRAPE_ORG_ID` 不被场景 custom_fields 覆盖 |
| T5 | 存量清理迁移 | `tests/mgmt/test_issue_4066_custom_fields_cleanup.py` | `src/domains/mgmt/migrations/autopilot_custom_fields_cleanup.py` + `src/api/db_setup.py` 注册 | autopilot 行与 scene advanced_options 非法项清零，合法项保留，幂等，post_check 通过 |
| T6 | 回归 | — | — | `pytest tests/kernel tests/mgmt tests/runtime tests/api/test_migration_track_guard.py -q` 全绿 |

## 依赖

T2/T3 依赖 T1 的白名单（先合并 T1 再跑 T2/T3 绿）。T5 独立。

## 完成定义

- 4 个新增测试文件全绿，既有 autopilot / org_analysis / 1811 相关测试无回归；
- 迁移守卫（版本唯一性/格式）通过；
- 自审查 5 轴 + pre-pr 增量检查（黑名单/敏感扫描）通过；
- PR 以 `arch-weekly-11-ljt` 为 base，已评论告知并 A2A 通知 QA。