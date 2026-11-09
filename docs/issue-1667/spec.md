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

## 实现偏离记录（回写，QA 两轴审查 PR #1069 要求）
- **排队载体偏离**：Spec 初稿写「复用 agent_tasks.queued」，实现改用 `events.status='queued'` + `payload._a2a_queue`。理由：零 schema 变更、天然具备 seq 语义，且不会被 worker claim；已在 plan.md 决策表同步。
- **终态兜底对账（新增，必须）**：drain 不仅挂接 `update_event_status/update_event_complete`，还通过 `reconcile_queued_a2a` 覆盖：①直接 SQL 写终态的旁路（runtime 任务级联、issues admin 确认）；②「活跃判定 → 排队落库」竞态窗口。patrol 主循环每轮兜底扫描一次，排队 A2A 不会永久滞留。
- **queue 幂等原子化（新增，必须）**：`append_queued_a2a_event` 在同一 db_lock/事务内完成「查重 + 插入」；无 idempotency_key 的并发同上下文请求不会产生多条排队行。
- **drain 安全**：查询按前序事件 (repo, issue, agent) 收窄；重入锁防止 `_fail_event → 终态钩子 → drain` 递归；异步上下文中的终态钩子经线程池执行，不阻塞终态写入。
