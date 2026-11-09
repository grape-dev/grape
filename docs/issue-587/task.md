# Task Breakdown: Issue #587

| # | 任务 | 文件 | 验证 |
|---|------|------|------|
| 1 | update_task_status 终态级联收敛 + converge_event_to_task | src/runtime/service.py | 单测：cancel/failed/completed 三态级联、终态保护 |
| 2 | sweeper 超时事件按任务终态映射收敛 | src/runtime/cleanup.py | 单测：cancelled 任务 → 事件 cancelled（非 failed） |
| 3 | 补偿重试入口 POST /tasks/{id}/converge-event | src/api/routes/runtime.py | 单测：端点调用 + 幂等 |
| 4 | 回报失败可观测记录 | src/runtime/service.py | 单测：errors 结构化追加 |
| 5 | terminate/redispatch 审计留痕 | src/api/routes/events.py | 单测：admin_action 事件含 actor/time/target/action |
| 6 | A2A 去重键抽取 + 回归锁定 | src/api/routes/webhook.py | 单测：终态不阻断 / 活跃阻断 |
| 7 | 文档 spec/plan/task | docs/issue-587/ | — |
| 8 | 自审查（5 轴 + 开源前置增量检查）→ 建 PR → 评论 → A2A QA → tasks complete | — | — |
