# Task: Issue #1667 任务拆解

- [x] T1 去重键引入 context/idempotency_key（`_a2a_context_key` / `_has_active_a2a_event`）。
- [x] T2 `A2AHandoffBody` 增加 `queue`/`force`，payload 兼容兜底。
- [x] T3 queue 分支落 `status='queued'` 事件，响应 `queued`/`reason`/`existing_seq`/`queue_seq`。
- [x] T4 force 分支跳过活跃去重并取消同上下文旧排队。
- [x] T5 queue 幂等：同上下文复用已排队记录。
- [x] T6 duplicate 响应补充 `queued=False`/`reason`，保持 #729 `original_event`。
- [x] T7 抽取 `run_a2a_dispatch` 供正常/排队共用；保留 `_on_handoff_dispatch` 兼容导出。
- [x] T8 终态 drain：`drain_queued_a2a` + 原子抢占，仅派发一次；失败走 `_fail_event`。
- [x] T9 `GET /api/a2a/status` 输出排队状态。
- [x] T10 新增测试覆盖上下文区分 / duplicate / queue / force / drain 幂等。