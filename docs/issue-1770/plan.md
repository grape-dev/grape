# Plan: Issue #1770

1. `src/api/a2a_dispatch_guard.py`（新增）
   - `resolve_scene`：Issue 场景 + pipeline nodes（复用 `get_issue_scene` / `get_pipeline_config`）。
   - `pipeline_order`：按 nodes 声明顺序并沿 `next` 链校正节点顺序。
   - `find_running_downstream`：同 Issue 同场景 running/dispatched 下游事件查询；目标为上游才命中。
   - `find_window_duplicate`：同 Issue + 同场景 + 同目标 agent 的时间窗去重（仅 completed 计入；failed/cancelled/timeout 允许立即重试）。
   - `find_active_duplicate`：短窗内同 Issue + 同场景 + 同目标 agent 活跃重复拦截（同上下文沿用 #1667 语义，queue/force 保留）。
   - `append_guarded_a2a_event`：同一 `db_lock` 内原子完成「门禁复查 + 落库」，消除并发 TOCTOU。
   - `window_key`：`issue + scene + target agent + 时间窗分桶` 的稳定哈希（仅观测/排查，不作为持久幂等键）。
   - `detect_dispatch_storm`：短窗派发次数告警（`A2A_DISPATCH_ALERT`，含 `event_skipped` 跳过次数）。
   - `record_skip`：写 `event_skipped` 审计，reason 携带引用 seq。
2. `src/api/routes/webhook.py`
   - 在既有活跃事件去重之前、`append_event` 之前调用门禁；`force=true` 越过。
   - 跳过响应按 kind 返回相关字段（公共 `skipped` / `reason` / `scene` / `window_key`）：`downstream_running` 另含 `downstream_event`；`duplicate_key` 另含 `existing_seq` / `original_event`；`active_dispatch` 另含 `existing_seq`（`_a2a_guard_skip_response`）。
   - 普通 a2a 落库持久化 Issue 场景维度 `scene_id`；pipeline nodes 为空时显式告警。
   - 派发成功后调用短窗告警检测（统计含本次派发）。
3. 测试 `tests/test_issue_1770_a2a_dispatch_guard.py`
   - 下游 running 跳过、下游结束后恢复、时间窗丢弃、失败后立即重试、活跃重复拦截、窗口过期恢复、force 越过、短窗告警（含跳过计数）、窗口键维度、并发原子落库。
4. 文档 `docs/issue-1770/{spec,plan,task}.md`。