# Task: Issue #337 — 任务拆解

| # | 任务 | 文件 | 依赖 | 验证 |
| --- | --- | --- | --- | --- |
| 1 | 后端领域函数：可移交目标计算（权限同源/排除当前/仅 vod） | src/api/issues_center.py | - | pytest targets 用例 |
| 2 | 后端端点：GET /{issue_id}/handoff/targets | src/api/routes/issues.py | 1 | pytest（含 403/空列表） |
| 3 | 后端端点：POST /{issue_id}/handoff（代理 a2a_endpoint） | src/api/routes/issues.py | 1 | pytest（成功/409/400/幂等） |
| 4 | 前端：列表模式详情操作区入口 | src/api/static/js/pages/issues.js | 2,3 | tests/js |
| 5 | 前端：弹窗目标列表 + 提交改走后端 + 二次确认 | src/api/static/js/pages/issues.js | 4 | tests/js |
| 6 | i18n zh/en 词条 + index.html 版本号 | locales/*/issues.json, index.html | 5 | tests/js |
| 7 | pytest 新增测试 | tests/test_issue_337_list_handoff.py | 1-3 | pytest 全绿 |
| 8 | JS 新增测试 | tests/js/test_issue_337_list_handoff.js | 4-6 | node 全绿 |
| 9 | 回归：现有 pytest + tests/js（329 前端测试同步版本号断言） | - | 7,8 | 全绿 |
| 10 | 自审查 → 原子提交 → PR（base=weekly-ljt）→ Issue 评论 → A2A QA | - | 9 | runbook Step 5-7 |
