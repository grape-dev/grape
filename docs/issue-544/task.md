# Task: Issue #544 拆解

| # | 切片 | 测试 seam | 状态 |
|---|------|-----------|------|
| 1 | L1 红：缺失闭合小时不再同步 raw-scan、pending 透出、后台回填补零样本小时 | `compute()` extra + rollup 行 | ☐ |
| 2 | L1 绿：slo_backfill.py + slo.py 改造 | 同上 | ☐ |
| 3 | L2 红：同键重复 POST 返回已有事件、坏键 400 | `/api/a2a` 响应 + events 行数 | ☐ |
| 4 | L2 绿：列/索引迁移 + append_event + endpoint 早退 | 同上 | ☐ |
| 5 | L3 红：异常留痕事件含指纹；GET status 命中 | events 行 + status 接口 | ☐ |
| 6 | L3 绿：留痕包裹 + status 路由 | 同上 | ☐ |
| 7 | L4 红：merged 无路由默认 sre；sre 禁用跳过；重试耗尽发 Issue 评论 | run_pipeline 路由 + 评论调用 | ☐ |
| 8 | L4 绿：路由兜底 + _notify_issue_broken_chain | 同上 | ☐ |
| 9 | docs：help/api/12-a2a.md + docs/openapi/12-a2a.md | 文档同步测试 | ☐ |
