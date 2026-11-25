# Task Breakdown: Issue #4103

- [x] 新增 `_set_issue_pending_review_with_prior`：写入前实读、幂等短路、CAS 条件写入、写后复读。
- [x] `_set_issue_pending_review` 收敛为布尔兼容包装。
- [x] `pending-review` 回执新增 `prior_status`，`status` 改为写后真实值，`changed` 改为本次真实写入（rowcount>0）。
- [x] 会话/通知副作用仅在真实置位 `pending_review` 时触发。
- [x] 新增四条验收测试（幂等零写入 / prior_status 一致 / 并发挤出复置 / CAS 未命中不误报不通知）。
- [x] 测试 teardown 清理探针触发器/表，避免跨模块污染。
- [x] 运行新增测试与相关既有 runtime 测试。
