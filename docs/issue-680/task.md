# Task — Issue #680

- [x] T1 helper：`_norm_issue_id` / `_max_issue_from_issues`（含 ##274 修复）
- [x] T2 `resolve_issue_id`：issues 兜底取最大 + 出口规范化
- [x] T3 `resolve_pr_issue_id`：优先级重排（分支 → 标题 → issues 最大 → API 同规则）
- [x] T4 `_resolve_event_issue_id` 统一入口（一次解析 + token 补偿）
- [x] T5 7 处调用点接入统一值
- [x] T6 新增 tests/test_event_pipeline_pr_issue.py（23 用例）
- [x] T7 回归：pipeline / 252 / 528 / 426 / 415 / 544 / 89 / qa_skill 与基线比对一致
- [x] T8 自审查（5 轴 + pre-pr 增量检查）→ 提交 → PR → A2A QA
