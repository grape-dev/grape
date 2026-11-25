# Plan: Issue #4170 — 交付终态终态抑制

## 方案概述
在 **派发入口单点短路** + **等待态恢复单点抑制** 两处复用同一个终态判定函数，
避免把终态逻辑散落到多条派发/写回链路。

## 终态判定（单点）
`src/domains/runtime/issues_center.py`：

- `_issue_has_merged_pr(repo_key, issue_number)`：查 `events` 表是否已有该 Issue 的
  真实 `pull_request_merged` 事件（口径对齐 `org_dispatch._pr_evidence`，兼容
  `issue_id` 带/不带 `#` 两种历史形态）。
- `_issue_repo_state_closed(repo_key, issue_number)`：用仓库 bot token 的
  `VcsApiClient.get_issue_state()` 读 VCS Issue 仓库态，`closed` 为真。
- `is_issue_delivery_terminal(repo_key, issue_number)`：`merged && closed` 双条件；
  任一读不到/异常 → `False`（fail-open，宁放过不错杀在途单）。
  reopen 后仓库态变回 `open` → 判定自然为假，无需单独 reopen 策略。

## 派发守卫（event_pipeline）
`src/domains/runtime/pipeline/event_pipeline.py` 的 `run_pipeline`：在 actor 解析之后、
2.0b 等待态恢复之前，对 `issue_comment` 调用终态判定；命中则：

1. 调 `event_store.append_terminal_suppressed_event` 落一行 `events(status='skipped',
   payload._terminal_suppressed)`，保留 `comment:<id>` 幂等键并补 `event_skipped` 审计；
2. 返回 `skipped=terminal_delivery_suppressed`，**不**进入路由/入队/等待态恢复。

守卫位置早于 2.0b，确保「0 复位」；写入失败时回退 `event_skipped`，审计不静默丢失。

## 复位抑制（issues_center）
`_resume_issue_waiting` 首行调用 `is_issue_delivery_terminal`，命中直接返回 `False`，
`stage`/`status` 均不写回（不把 `pending_review` 打回 `processing`）。

## 事件留痕语义
`append_terminal_suppressed_event` 以 `agent_type=""` 落库；`pipeline_status.compute_node_statuses`
只按非空 `agent_type` 聚合最新事件，因此终态评论留痕不会污染节点进度/状态。
刷新后的基线不再包含 `pipeline_status.DEDUP_LEFTOVER_STATUSES`/`is_dedup_leftover_event`
（该去重能力不在本基线上），故原计划的该 docstring 同步随基线架构变更移除，不引入范围外代码。

## 影响与风险
- 终态判定误判风险：双条件 + fail-open + 非终态回归用例覆盖；
- VCS 读开销：仅在已存在 merge 事件时才发起一次 `get_issue_state` 读；
- 不改动去重、回退治理等其他单职责（边界外）。