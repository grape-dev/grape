# Task: Issue #341 — 任务拆解

- [ ] T1 app.js：`view === 'tasks'` 透传 param 到 `renderTasks(param)`
- [ ] T2 issues.js：`renderTasks(param)` 解析 `^\d+$` 深链编号，设置 `_pendingIssueNumber` + `_deeplinkIssueNumber`
- [ ] T3 issues.js：`loadTasks()` 深链命中/无匹配/请求失败三分支容错（Toast + 回退列表 + 标志清理）
- [ ] T4 i18n：zh/en tasks.json 新增 `deeplink_not_found`
- [ ] T5 新增 tests/frontend/test_issue_341_tasks_deeplink.js 覆盖上述分支
- [ ] T6 回归：现有 tests/frontend + tests/js 相关用例全部通过
- [ ] T7 自审查（5 轴）→ 提交 → PR → 评论 → A2A QA
