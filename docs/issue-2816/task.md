# Issue #2816 任务拆解

| # | 任务 | 验收 |
|---|------|------|
| T1 | 终态判定纯函数（labels / 标记行 / 载体 PR 识别） | 单测：label 命中、marker 命中、intersect、放行 |
| T2 | A2A 派发入口短路 + `event_skipped` 审计 + 置人工态 | 集成测试：coding 派发被跳过，issue 状态 awaiting_input |
| T3 | 载体 PR 解析与 VCS 读取（best-effort + 缓存） | 失败放行；显式 carrier 优先 |
| T4 | intersect 人工处置（new_carrier/reassign/close） | 单测：动作校验、meta 落库、状态恢复 |
| T5 | 路由暴露 + 文档 | `POST /api/issues/{id}/terminal-disposition` 可用 |