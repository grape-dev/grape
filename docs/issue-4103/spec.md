# Spec: Issue #4103 — pending-review changed 语义与 prior_status 回执

## Objective

- 让 `grape issues pending-review` 回执中的 `changed` 唯一对应本次是否发生真实状态写入。
- 回执新增 `prior_status`（写入前实读状态），与 `status`（写后实读状态）成对返回，形成
  `prior_status → status` 可对账闭环，调用方无需额外 `issues status get` 推断。

## Scope

### In Scope

- `pending-review` 状态写入前在同一事务/锁内实读 `prior_status`。
- 已在 `pending_review` 且 review_points/references 等价时幂等短路：不执行 UPDATE，
  返回 `changed:false`；审核点内容变更时仅刷新快照（沿用 #4063 口径），状态不迁移。
- 从 `active/processing/awaiting_input` 复置时使用条件写入（`WHERE status=prior_status`），
  避免读改写竞态覆盖新状态。
- 写后复读 `status`；`changed` 以本次 UPDATE 的真实写入（`cursor.rowcount > 0`）判定，
  不按 `prior_status != status` 反推——并发 CAS 未命中时 `changed:false` 且不触发通知。
- 回执 `{ok, issue_id, prior_status, status, changed}`，保持既有字段兼容。

### Out of Scope

- 不改 #4079 幂等不回声 `review_points` 口径。
- 不改 #4022/#4076/#4026/#4007 的最终一致性/评论事件清除审核点根因。
- 不改派发侧「抑制同轮在跑派发消费审核点」策略。
- 不改 CLI 子命令名、参数与既有回执字段语义。

## Acceptance Criteria

1. 写入前 `status=pending_review` 且无并发事件时，返回 `changed:false` 且无状态写入。
2. 回执 `prior_status` 与写入前 `issues status get.status` 完全一致。
3. 审核点被并发派发挤出为 `active` 后复置，返回 `prior_status=active` +
   `status=pending_review` + `changed:true`。
4. 读后 UPDATE 前被并发方抢先置为 `pending_review`（CAS 命中 0 行）时，
   返回 `changed:false`、`idempotent:true`，且不重复触发审核通知。
5. 新增字段兼容 `ok/issue_id/status/changed`，现有测试通过，新增四条验收测试。
