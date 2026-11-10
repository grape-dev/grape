# Plan: Issue #1770

1. `src/api/a2a_dispatch_guard.py`（新增）
   - `resolve_scene`：Issue 场景 + pipeline nodes（复用 `get_issue_scene` / `get_pipeline_config`）。
   - `pipeline_order`：按 nodes 声明顺序并沿 `next` 链校正节点顺序。
   - `find_running_downstream`：同 Issue 同场景 running/dispatched 下游事件查询；目标为上游才命中。
   - `find_window_duplicate`：同 Issue + 同场景 + 同目标 agent 的终态派发时间窗去重（活跃/排队事件沿用既有 #1667 语义）。
   - `idempotency_key`：`issue + scene + target agent + 时间窗分桶` 的稳定哈希。
   - `detect_dispatch_storm`：短窗派发次数告警（`A2A_DISPATCH_ALERT`）。
   - `record_skip`：写 `event_skipped` 审计，reason 携带引用 seq。
2. `src/api/routes/webhook.py`
   - 在既有活跃事件去重之后、`append_event` 之前调用门禁；`force=true` 越过。
   - 跳过响应返回 `skipped` / `reason` / `scene` / `downstream_event` / `existing_seq` / `original_event`。
   - 派发成功后调用短窗告警检测（统计含本次派发）。
3. 测试 `tests/test_issue_1770_a2a_dispatch_guard.py`
   - 下游 running 跳过、下游结束后恢复、幂等窗口丢弃、窗口过期恢复、force 越过、短窗告警、幂等键维度。
4. 文档 `docs/issue-1770/{spec,plan,task}.md`。