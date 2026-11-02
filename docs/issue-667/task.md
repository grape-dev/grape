# Task: Issue #667

- [x] 时序排查结论（webhook → issue 表落行 → GRAPE_ISSUE_ID 注入链路与 404 发生环）
- [x] event_pipeline：`fetch_issue_via_vcs` + `_inject_issue_pr_env` 空值补建
- [x] routes/issues：detail 兜底解析 + 404 语义区分
- [x] routes/issues：awaiting-input `issue_number` 兜底解析
- [x] help/api/11-issues.md 404 降级指引
- [x] 新增测试 tests/test_issue_667_detail_fallback.py，回归相关测试
- [x] 提交、push、建 PR（base=weekly-08-mys）、Issue 评论、A2A QA、tasks complete
