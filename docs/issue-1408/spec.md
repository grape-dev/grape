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

## 复审补充（PR #1009 审查意见，2026-11-12）

首版仅实现后端状态机出口与 `PUT /issues/{id}/status`，页面端无入口、管理员撤销被 403 拒绝，
与 Objective「允许需求属主手动撤销」名实不符。补充范围：

- **页面入口**：需求详情页任务操作区新增「撤销完成」按钮（`src/api/static/js/pages/issues.js`），
  仅 `status === 'completed'` 渲染；可见范围 = 需求本人 或 仓库管理员（复用 `_isRepoAdmin393`），
  非本人且非管理员不渲染；带 hover 过渡、双语 tooltip 功能说明、`_grapeConfirm` 二次确认，
  成功后 toast 并刷新列表与流水线区。原 `@deprecated` 死代码 `changeTaskStatus` 删除。
- **管理员撤销路径**：`PUT /issues/{id}/status` 对 `completed → pending_review` 增加仓库
  Owner/Admin 放行——参照 `admin-confirm-done` 的 `check_repo_access` + `get_repo_admin_identity`
  fail-closed 真实角色校验；本人路径不触发角色查询，其余迁移仍严格仅限本人。
- **审计**：新增 `actor_role`（owner/admin）与 `role_source` 字段，管理员撤销记录真实角色来源。
- **缓存/双语**：`index.html` 同步 bump `issues.js?v=138`、`app.css?v=109` 并更新前端缓存版本
  ratchet 冻结映射；`tasks.json` zh/en 同步四条文案 key。