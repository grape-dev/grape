# Spec: Issue #1408 — 已完成需求可手动撤销回待审核

## Objective
允许需求属主把已到终态的需求（`status=completed`、`stage=done`）手动撤销回「待审核」（`status=pending_review`），重新进入审核/流水线流转。解决 `completed` 为终态无出口、误完成/需返工只能重建需求的问题。

## Scope

### In Scope
- 状态机新增 `completed → pending_review` 出口。
- 撤销时 stage 由实际当前值同步回退为 `pending_review`，仅更新 status/stage/完成字段/`updated_at`，保留 `assignee`/`owner_agent`/`a2a_seq` 等归属字段。
- 撤销时清空 `completed_at`、`completed_reason`，并刷新 `updated_at`。
- 撤销写路径带 `AND status='completed'` 并发守卫：读后写前状态已被并发修改时不覆盖，返回 409。
- 操作审计留痕（操作者、需求定位、旧/新状态、撤销前完成时间与原因、时间）；审计为 best-effort，失败仅告警、不回滚状态。
- 权限：仅需求属主，且仅用户身份（JWT/`grape_`）；会话 token（`tk_`）返回 403。
- 幂等：目标状态已是 `pending_review` 时重复提交成功；非法迁移仍 400。

### Out of Scope
- 重新打开已关联的 VCS Issue。
- `completed → processing` 等其它终态出口。
- 自动/批量撤销能力。
- 发布子单（`release_*` 列）的连带清理。

## Acceptance Criteria
- [x] `completed` 需求更新为 `pending_review` 返回成功，回读 `status=pending_review`。
- [x] stage 由 `done` 回退为 `pending_review`，不再停留在 `done`。
- [x] `completed_at`、`completed_reason` 清空，`updated_at` 刷新，审计留痕可查（含实际 from_stage、撤销前完成信息）。
- [x] 非属主、会话 token 调用返回 403；非法迁移仍 400；并发态变更返回 409 且不写审计。
- [x] 重复提交同一目标状态幂等成功。
- [x] 回归：`pending_review → processing/completed/cancelled`、`cancel`/`resume` 不受影响。
- [x] 现有测试全部通过，新增测试覆盖新功能（`pytest tests/ -v`、`pytest tests/ --cov`）。