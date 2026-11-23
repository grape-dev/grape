# Spec: Issue #3834 — 接管让渡后 cascade 补偿事件断掉 A2A 链

## Objective

修复本地接管（takeover）coding 节点 `finish` 交回后，让渡时由 `cancel_task` 产生的
cascade 补偿事件（`REASON_SYNTHESIZED`、`is_agent_conclusion=false`、`task_status=cancelled`）
不具真实 Agent 结论资格，导致 `stage=qa` 已写入但 QA 任务永不派发的 P0 缺陷。

成功标准：接管单 `finish` 后 QA 自动派发，无需外部 `trigger_pipeline` 重跑整链。

## Scope

### In Scope

- 接管 finish 写入真实结论时，把事件补偿归因改判为 `REASON_BACKFILLED`（已回填真实结论），
  事件重新具备触发下一节点资格，并携带 `structured_output`。
- `mark_task_takeover` 未生效（事件停在 `cancelled`）时，接管 finish 仍可授权改写为 `completed`。
- 场景节点写法非标准导致 `_advance_stage` 解析不到下一节点时，补一次节点链解析，避免静默断链。
- `events` 表补齐 cascade 归因列（老库幂等迁移）。
- 回归用例：接管 finish → QA 派发；未接管普通事件不产生补偿归因。

### Out of Scope

- P1 三项：`takeover_start` 远端状态体检、roadmap 数据源对齐、MR 合入自动放行。
- 全量恢复历史 #1938 的展示层/可观测性集成（本分支基线已缺失，另行建单）。

## Acceptance Criteria

- [ ] 接管 coding finish 后 QA 自动派发，无需 `trigger_pipeline`。
- [ ] 接管事件归因 `REASON_BACKFILLED`、`is_agent_conclusion=true`，携带 `structured_output`。
- [ ] 非补偿（普通）事件不被误标为系统补偿。
- [ ] 新增回归用例通过；本次改动范围内既有接管用例不回归。