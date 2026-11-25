# Spec: Issue #4170 — 交付终态（PR merged + Issue closed）后 issue_comment 派发与审核点复位抑制

> 来源：Issue #4170「Spec Document」评论（issue agent 已评级 ready，本文为开发侧对齐稿，不重复展开需求）。

## 需求（同 Issue Spec）
Issue 达成交付终态（承载 PR 已 merged **且** Issue 仓库态 closed）后，`issue_comment`
事件不再派发新的 Agent 任务，也不再复位 `stage`/`task_status`；事件仍需记录、可查（审计不丢）。

## 现状与根因
- 源事故（跨仓生产事故，仓库与编号已脱敏）：承载 PR 合入且 Issue 关闭后，QA/SRE 仍互发「收讫/同值/止环」评论；
  每条评论再生 `issue_comment` 事件，`run_pipeline` 的等待态恢复
  （`_resume_issue_waiting`: `pending_review → processing`）把审核点反复复位，形成自激链。
- 平台此前只在 stage=`done` 时不分发，终态但 stage 停在 `pending_review` 的单没有整体抑制。

## 范围
### In Scope
- `issue_comment` 派发入口终态守卫（终态仅记录、不派发）；
- 等待态恢复（stage/task_status 写回）终态抑制（终态不写回）；
- 终态判定单点函数：Issue closed + 承载 PR merged 双条件；
- 回归：非终态派发/复位行为不变。

### Out of Scope
- comment id / head SHA 维度去重（#4111/#4138/#4114/#4133）；
- pending_review 回退 active 的通用治理（#4143/#3936/#3981）；
- reopen 完整策略（reopen 后仓库态回 open 即自然脱离终态）。

## 验收
- [ ] 终态 Issue 新评论 0 派发（无 `agent_tasks`）；
- [ ] 终态 Issue 评论 0 复位（`stage`/`status` 原样保留）；
- [ ] 终态评论事件仍落库可查（events status='skipped' + event_skipped 审计）；
- [ ] 非终态 Issue 派发与复位回归不变；
- [ ] 新增单测覆盖终态抑制 + 非终态回归；现有测试不因本改动新增失败。