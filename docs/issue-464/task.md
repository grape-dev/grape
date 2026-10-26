# Task: Issue #464 — 任务拆解

| # | 任务 | 产出 | 状态 |
|---|------|------|------|
| 1 | 写失败测试（红）：CSS 静态断言 hover 桥 + recalled 链接配色 + #442 回归锚点 | `tests/frontend/test_chat_recall_464.js` | done |
| 2 | app.css 实现（绿）：`.msg.r::before` hover 桥 + `.chat-msg-recalled a` 配色覆盖 | `src/api/static/css/app.css` | done |
| 3 | 回归：`test_chat_recall_442.js` + 全部 frontend 测试 | 全绿 | done |
| 4 | 自审查 → 推送 → PR（base=weekly-XX）→ Issue 评论 → A2A QA | PR | done |
