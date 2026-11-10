# Task: Issue #1806 任务拆解

- [x] T1 新增折叠任务单源判定函数 `is_fold_task`（`sweeper-v2-<任意>-fold<数字>`）。
- [x] T2 context 四要素归一与缺失清单：`normalize_fold_context` / `missing_fold_context_fields`。
- [x] T3 400 错误体构造：含 idempotency_key、缺失字段、schema 示例、hint。
- [x] T4 配置开关 `GRAPE_A2A_FOLD_CONTEXT_ENFORCE` 支持降级放行 + 结构化告警事件。
- [x] T5 `a2a_endpoint` 接入守卫（幂等命中短路后、事件创建前；非折叠任务旁路）。
- [x] T6 单测覆盖折叠任务空/缺项拦截、合法文本/结构化放行、非折叠幂等不变。
- [x] T7 回归：现有 A2A 相关测试全部通过。