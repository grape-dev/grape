# Task: Issue #683

- [x] errors.py：`extract_allowed_values` + `validation_error_response`
- [x] main.py：注册 `RequestValidationError` 全局 handler
- [x] 新增 tests/test_issue_683_422_detail.py（非法值 422 详情 + 合法值不回归）
- [x] 全量相关测试回归（test_issues_api.py 等）
- [x] 自审查（5 轴 + pre-pr 增量检查）→ 建 PR → Issue 评论 → A2A QA
