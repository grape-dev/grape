# Plan: Issue #3834 — 接管 finish 回填真实结论并保证 A2A 派发

## 断链点

1. `start_takeover` → `cancel_task` 将 coding 事件级联收敛为补偿终态
   （`cascade_reason=REASON_SYNTHESIZED`，`is_agent_conclusion=false`，`task_status=cancelled`）。
2. `mark_task_takeover` 把事件状态拉回 `takeover`，但不清理补偿归因。
3. `finish_takeover` 通过 `update_event_complete(allow_from_takeover=True)` 写入真实结论；
   旧实现存在两个缺口：
   - 若 `mark_task_takeover` 未生效/竞态，事件仍是 `cancelled`，终态保护直接跳过写入 →
     事件保持补偿文案、无 `structured_output`；
   - 即便写入成功，补偿归因仍为 `REASON_SYNTHESIZED`，展示层仍判为非 Agent 结论。
4. 下一节点派发依赖 `_advance_stage` 解析；节点写法非标准时解析为空会静默不派发。

## 方案

采用 A + B 组合（Issue 允许择一或组合）：

- **B（回填）**：`event_store.update_event_complete` 增加窄口子——仅
  `allow_from_takeover=True` 且 `cancelled → completed` 放行，保证接管 finish 的授权终态写入生效。
  `takeover.finish_takeover` 在写入前快照事件补偿状态，写入后调用
  `mark_backfilled_conclusion` 将归因改判为 `REASON_BACKFILLED / triggered_by=takeover_finish`，
  使 `cascade.display_meta` 返回 `is_agent_conclusion=true`。
- **A（派发）**：保留 finish 的 `/api/a2a` 主动派发；新增 `_next_agent_from_scene` 兜底解析
  （id/tag/role 多口径 + `get_next_node`），目标为空时返回显式 `next_agent_unresolved`
  而非静默成功。

## 数据/迁移

`events` 表新增 cascade 归因列（`cascade_reason` / `report_missing` / `triggered_by` /
`cascade_task_status` / `cascade_prev_event_status`），新库写入 `schema_ddl`，老库走
`migrations_ddl.ALTERS_SESSION_EVENT_COLUMNS` 幂等补列。

## 测试

- `tests/runtime/test_issue_3834_takeover_cascade_chain.py`
  - 让渡补偿事件 → finish 后改判 backfilled、structured_output 完整、QA 派发参数正确；
  - 事件停在 `cancelled` 时 finish 仍能改判 completed 并派发；
  - 非补偿事件不被改判。
- 既有接管用例（`tests/test_takeover_flow.py` 等）作为 #59 类未接管链路对照。