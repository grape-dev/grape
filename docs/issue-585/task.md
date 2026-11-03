# Task: Issue #585

- [x] T1 Bug 1：`create_pull_comment` GitHub 通道改走 `/issues/{n}/comments`（src/api/git_repo_api.py）
- [x] T2 Bug 2：`list_pull_files` 增加 `page`/`per_page` 透传，默认 per_page=100（src/api/git_repo_api.py）
- [x] T3 端点 description 更新（Issue #585 契约说明）
- [x] T4 runbook 错误排查表补充 422 降级条目 + 大 PR 兜底说明（.grape/skills/qa/**/runbook）
- [x] T5 新增测试：GitHub 通道评论端点分流 / GitCode 保持不变 / 分页透传 + 非法值 422
- [x] T6 回归：test_git_repo_api.py 全量（除基线既有失败）、outbound_ledger / 372 marker / openapi
- [x] T7 提交并创建 PR（base=weekly-08-mys，head=feat/issue-585）
