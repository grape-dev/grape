# Issue #3744 任务拆解

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-22

| # | 切片 | 交付物 | 验证 |
|---|------|--------|------|
| 1 | 红：来源短路回归用例 | `tests/runtime/test_issue_3744_autopilot_scene_reconcile.py` | `pytest tests/runtime/test_issue_3744_autopilot_scene_reconcile.py` 先红 |
| 2 | 绿：共享常量 + 判定 + 入口短路 + WARNING 字段 | `scheduler.py`、`event_pipeline.py` | 新用例转绿 |
| 3 | 全量回归 | — | `pytest tests/runtime/test_issue_1745_scene_env_truth.py tests/runtime/test_issue_528_pr_sub_env_integration.py` 等通过 |
| 4 | 自审查 + PR 增量检查 | 5 轴审查 + 敏感扫描 | `origin/arch-weekly-11-ljt...HEAD` |
| 5 | 建 PR + Issue 评论 + A2A QA | PR / 评论 | 收尾 `grape tasks complete` |

## 原子提交

1. `docs(#3744): 补充 autopilot 场景校正短路 Spec/方案/任务拆解`
2. `test(#3744): autopilot 场景校正短路 先写失败用例`（红）
3. `fix(#3744): autopilot 任务场景校正按来源短路并增强缺行告警`（绿）