# Spec: Issue #1667 — A2A 同 issue+agent 运行中时新 handoff 被静默去重且无排队能力

> 来源：Issue Agent Spec（Issue 评论 2026-11-09）| Coding 整理

## Objective
A2A handoff 支持按上下文区分任务；命中同 issue+agent 活跃事件时提供排队（前序终态后自动派发）或强制下发能力，并在响应中明确 `duplicate_a2a` / `queued` 语义。

## Scope
### In Scope
- `POST /api/a2a` 去重判定引入 context / idempotency_key 维度（复用已有 idempotency_key 链路）。
- 命中活跃事件时支持 `queue=true`（排队）与 `force=true`（立即新建事件）。
- 响应区分 `skipped=duplicate_a2a` / `skipped=queued`，明确 `existing_seq`（前序事件）与 `queue_seq`（排队事件）。
- 前序事件终态时自动派发排队事件，且仅派发一次。
- `GET /api/a2a/status` 可查询排队状态。
- 保持 #729 `original_event` 字段向后兼容。

### Out of Scope
- 跨场景 `duplicate_handoff`（scene 级去重）语义不变。
- 不改造 worker / engine 调度机制。

## 验收标准
- [x] 同 issue+agent、不同 context/idempotency_key 的 handoff 不再返回 `duplicate_a2a`。
- [x] `queue=true` 命中活跃事件返回 `queued` + `queue_seq`，前序终态后自动派发且仅一次。
- [x] `force=true` 可立即新建 A2A 事件。
- [x] 响应区分 `duplicate_a2a` / `queued`，并说明 `existing_seq` 语义。
- [x] 默认行为与 #729 `original_event` 保持向后兼容。
- [x] 现有测试通过，新增测试覆盖上述场景。