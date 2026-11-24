# Spec: Issue #4024 — pending_review 审核点态读回口径统一

> 承接 Issue 内已发布 Spec（评论 id 203175912），本文档记录 coding 实现阶段的终裁口径。

## Objective

统一服务端 `pending_review` 审核点态的读回口径：`issues pending-review` 置位落库、
`issues status get` 立即读回、`issues detail` / pipeline 的 `review_status` 聚合读回
共用同一状态判定，消除置位响应与读回读数分歧（含双 runtime 对账场景）。

## Scope

### In Scope

- `issues status get` 新增显式审核点态字段 `review_status`（方案 B），`status`
  继续保留任务态枚举语义不变。
- `issues pending-review` 响应回读落库真值（不再硬编码 `status`），并携带同源
  `review_status`，使置位响应与立即读回逐字段一致。
- `issues detail` 已知内部主键时以该 `issue` 行为单一事实源派生 `task_status` /
  `pipeline.review_status`，避免多平台别名行聚合顺序不一致导致两侧打架。
- 契约/文档注明 `review_status` 的聚合口径来源。
- 「置位 → 立即读回」与「双 runtime（status get ↔ detail/pipeline）对账」回归用例。

### Out of Scope

- 写后读一致性抖动（#4022）、`review_status`/`stall` 字段存在性不稳定（#3972）、
  评论事件重置审核点（#4007/#3981）、CLI 客户端判据改造 —— 均独立立项。

## 事实源与聚合口径（终裁）

- 单一事实源：`issue` 表同一行（内部主键 `issue.id`）的 `status` + `stage`。
- 派生函数：`src/api/pipeline_status.derive_review_status(stage, task_status)`。
- 优先级：`pending_review` > `cancelled` > `completed`/`stage=done` >
  `awaiting_input` > 在途（`active`/`processing`/任一非空 stage）> `unknown`。
- 字段命名：`review_status`（与 `issues detail.pipeline.review_status` 一致）。
  枚举：`pending_review` / `completed` / `cancelled` / `awaiting_input` /
  `in_progress` / `unknown`。

## Acceptance Criteria

- [x] pending-review 置位后立即 `issues status get`，`status` 与 `review_status`
      均与置位响应一致（不再恒为 active / in_progress）。
- [x] `issues detail.pipeline.review_status` 置位后为 `pending_review`，不滞留
      `in_progress`。
- [x] 双 runtime 对账（status get ↔ detail/pipeline）两侧 `review_status` 一致。
- [x] 契约/文档注明聚合口径来源。
- [x] 新增回归用例全部通过，既有 `#539` / `#2695` / `#2111` 等测试通过。