# Spec: Issue #2381 — review-pass 在 admin redispatch 后恒为 noop

## 背景
上一轮 qa 已把 Issue 置为 `pending_review`。管理员对原事件执行 redispatch 后，
`issues review-pass --target done` 恒返回 `changed:false` + `noop:not_pending_review`，
Agent 误以为「审核已完成」而漏登记。

## 根因
1. **末节点判据不一致**：`_is_last_stage_node`（review-pass 末节点放宽）只认
   `next=="done"`；`_pipeline_last_node_check`（自动结单）同时接受 `next==""`。
   当场景末节点 `next` 为空串时，review-pass 的末节点放宽失效，回落到
   「非末节点仅接受 pending_review」的严格通道。
2. **redispatch 重放旧评论事件触发 resume**：终态事件重派走 `run_pipeline` 重放，
   原 `issue_comment` 事件被重新处理，2.0b「澄清/审核回复自动恢复」再次执行
   `_resume_issue_waiting`，把 `pending_review` 翻成 `processing` 并把
   `stage=pending_review` 回落到 `owner_agent`，审核点被清掉。

## 目标
- 末节点判据统一：`next in ("done","")` 视为末节点。
- redispatch 重放旧事件时保留审核点：`_pending_review` 不被再次 resume。
- `noop` 原因码可区分「审核点已因 redispatch/用户回复失效」。
- 非末节点 `target=done` 仍只接受 `pending_review`，防止中间节点误终态。

## 范围
**In Scope**
- `src/domains/runtime/issues_center.py`：末节点公共判据、resume 保留开关、noop 原因码。
- `src/domains/runtime/pipeline/event_pipeline.py`：`run_pipeline(replay=...)` 透传。
- `src/domains/runtime/routes/events.py`：redispatch 重放时传 `replay=True`。
- `tests/runtime/test_issue_2381_review_checkpoint.py`：单测与回归。

**Out of Scope**
- 不改 review-pass CLI 契约（`evidence` / `target`）。
- 不改 qa 门禁规则。
- `qa-runbook` Step 6b 四态判定表属技能资产仓，单独跟踪。

## Acceptance Criteria
1. `pending_review` → redispatch → `review-pass target=done` 返回 `passed_done`
   或可区分的失效原因码，不再恒 `noop:not_pending_review`。
2. `_is_last_stage_node` 对 `next==""` 与 `next=="done"` 行为一致，单测覆盖
   `done` / 空串 / 未知 / 指向不存在节点四种情形。
3. `qa-runbook` Step 6b 交付项另行跟踪（本仓不含该文档）。
4. 非末节点 `target=done` 仍不误终态中间节点；现有测试全通过。