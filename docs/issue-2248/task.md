# Task: Issue #2248

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| T1 | force/queue/ctx_key 前移解析 + 终态命中 force 重派 | `src/api/routes/webhook.py` | done |
| T2 | 命中响应补 `existing_status` / `replayable`（前置 + 并发回查统一） | `src/api/routes/webhook.py` | done |
| T3 | 契约 schema 与 help/openapi 文档同步判定表与轮次键建议 | `contracts/`, `help/api/12-a2a.md`, `docs/openapi/12-a2a.md` | done |
| T4 | 回归单测（force 重派 / 无 force 原事件 / 新轮次键 / 活跃命中安全） | `tests/test_issue_2248_a2a_idempotency_force_replay.py` | done |
| T5 | 运行相关 pytest + 自审 + pre-pr 增量检查 | — | done |