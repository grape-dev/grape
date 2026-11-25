# Issue #3354 任务拆解

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1 | 建仓托管分支重试循环（3 次，5s/15s/45s，仅 repo_not_found） | `src/domains/mgmt/routes/repos.py` | 已完成 |
| 2 | 终态失败落库：原因 + 重试次数写入 `host_error` | `src/domains/mgmt/routes/repos.py` | 已完成 |
| 3 | 失败路径复用幂等 find/setup webhook 语义 | `src/domains/mgmt/routes/repos.py` | 已完成 |
| 4 | dashboard 透出 `host_step` / `host_error` | `src/domains/insight/routes/dashboard.py` | 已完成 |
| 5 | 前端失败态文案与「托管失败，点击重试」 | `src/api/frontend/app/src/views/DashboardView.vue` | 已完成 |
| 6 | mock VCS 延迟就绪回归用例 | `tests/mgmt/test_issue_3354_host_after_create_retry.py` | 已完成 |
| 7 | 相关既有用例回归 | `tests/` | 已完成 |