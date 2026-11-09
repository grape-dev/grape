# Task: Issue #977

- [x] service.py：`list_wrote_files` 派生查询（TDD RED→GREEN）
- [x] task_logs.py：`/info` 响应 + `responses` 契约 schema + docstring
- [x] help/api/09-task-logs.md 文档同步
- [x] 单测 2 条（write 57KB + patch / 无 write 空 list）
- [x] 契约验证：`/openapi.json` 的 task_logs_info 含 wrote_files；test_api_docs_sync（本仓基线即有 2 项与本次无关的既有失败，净改动不引入新失败）
- [x] 相关存量测试回归（test_task_messages / test_issue_701 / test_check_openapi_endpoints / test_openapi）
