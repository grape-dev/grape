# Issue #600 任务拆解

- [x] T1 docs：spec.md / plan.md / task.md
- [x] T2 后端：events.py 三处重派路径改 `_mark_event_redispatched`（cancelled 取代 failed）
- [x] T3 后端：issues.py 详情端点 events-first
- [x] T4 前端：events.js 重派/本机重试跳转新 seq
- [x] T5 前端：pipeline.js + app.js 流水线页轮询刷新
- [x] T6 前端：issues.js 任务详情流水线卡片轮询
- [x] T7 测试：tests/test_issue_600_redispatch_status.py + 回归
- [x] T8 自审查（5 轴 + pre-pr 增量检查）→ 建 PR → 评论 → A2A QA → complete
