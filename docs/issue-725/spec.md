# Issue #725 Spec — pending-review 重复调用幂等语义

> 来源：Issue #725（user feedback 后台 id 384）；沿用 issue Agent 已发布 Spec（目标分支 weekly-09），本文件为开发落档副本。

## 目标
- `POST /api/issues/{issue_id}/pending-review` 在 Issue 已处于 `pending_review` 状态时，重复调用短路返回 **200 幂等响应**（含 `status` + `idempotent: true` 字段），不再出现 405/非幂等语义。
- runbook/API 文档增补「重复调用处理指引」：已是 pending_review → 200 幂等，`changed=false`、`idempotent=true`，不重复写会话/发通知。

## 现状
- 端点对 `completed/cancelled` 已有 200 短路（`changed=false`），但对「已是 pending_review」的重复调用无显式短路：落入 `_set_issue_pending_review` 原子迁移失败分支（`changed=false`），响应无幂等标识，调用方（CLI/Agent）无法区分「重复幂等」与「迁移失败」，对外表现为 405 端点拒绝/错误码语义不友好（用户反馈 id 384）。

## 方案
1. `issue_pending_review` 在解析出 `it["status"] == "pending_review"` 时**立即短路**返回：
   `{"ok": true, "issue_id": ..., "status": "pending_review", "changed": false, "idempotent": true}`
   —— 不写会话审核点、不触发飞书通知（重复调用不重复打扰用户）。
2. 路由 description 与 `help/api/11-issues.md` 同步增补幂等语义与重复调用指引。
3. `completed/cancelled` 短路响应补充 `idempotent: true`（同为「已是目标/终态」的幂等语义，保持字段一致）。

## 范围（In / Out）
- In：`src/api/routes/issues.py` 幂等短路；`help/api/11-issues.md` 指引；测试覆盖幂等分支。
- Out：409 方案（Spec 已选 200 幂等）；review-pass/review-reject 既有幂等逻辑；前端 UI。

## 验收
- 已是 pending_review 时重复调用 → 200，`status=pending_review`、`changed=false`、`idempotent=true`，且不产生会话/通知副作用。
- 首次调用（active/processing/awaiting_input → pending_review）行为不变（`changed=true`）。
- 新增测试覆盖幂等分支并通过；原有测试全量回归。
