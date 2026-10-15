# Tasks: Issue #340

- [x] T1 文档：spec.md / plan.md / task.md
- [x] T2 src/repo_cleanup.py：cascade_cleanup_repo_data（事务化 + dry-run + 补偿 + 审计）
- [x] T3 src/auth/audit.py：新增 repo_cascade_cleanup 事件类型
- [x] T4 repos.py：/repos/remove 与 _probe_stale_repos 接入级联清理；_cleanup_repo_data 兼容包装
- [x] T5 webhook.py：仓库删除事件识别 + 等价清理（GitCode/GitHub 双路径）
- [x] T6 api_client.py：repo_exists() 探测
- [x] T7 前端 locales：confirm_remove 明确"将清空全部相关数据"
- [x] T8 测试：test_repo_cascade_cleanup.py（两触发路径 + 事务回滚 + dry-run + 审计）
- [x] T9 全量回归：现有测试通过
