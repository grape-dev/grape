# Spec: Issue #4340 — 停机汇总行把候选数当失败数

来源：Issue #4340 原始描述 + issue Agent 已发布 Spec（Issue 评论 2026-11-25 09:18）。
本文件为 coding 侧的落地摘要，权威需求以 Issue 内 Spec 评论为准。

## Objective

`terminate_active_agents` 的停机汇总行只统计**真实被判 failed 的 DIRECT 任务**，
并显式输出 REMOTE `pending_self_report` 数量；候选数不再冒充失败数。

## 现状（缺陷）

- `src/domains/runtime/agents/executor.py:378`：`marked {len(stale_tasks)} stale task(s) as failed`
  用候选总数当失败数；
- REMOTE 分支（`credential_id>0`）实际置 `pending_self_report` 并 `continue`，不计失败；
- 生产证据：单窗口 9/9 全 REMOTE，DB 实况 `failed=0`，汇总行却报 9 failed；24h 内该前缀 93 行。

## 期望行为

1. 汇总行始终输出中性三字段：
   `terminate_active_agents: shutdown sweep candidates=N failed_direct=N pending_self_report=N`；
2. 仅当 `failed_direct > 0` 时额外输出 `marked N stale task(s) as failed`（旧语义只在真失败时出现）；
3. 保留现有逐条 REMOTE / DIRECT 明细行；
4. 计数来源 = DIRECT 判死 UPDATE 的 `rowcount`（并发自报导致 0 行不计）。

## Scope

- In：`executor.py` `terminate_active_agents` 计数与汇总文案 + 停机路径单测。
- Out：`cleanup.py` sweeper 回收语义（#4303 / PR #2138）、`pending_self_report` 超时兜底、
  巡检看板与日报生成器改造（本 Issue 只提供口径切换点）。

## Acceptance Criteria

- [x] REMOTE-only：`failed_direct=0` / `pending_self_report=N`，全程无 `marked ... as failed`
- [x] DIRECT-only：`failed_direct=N`，与 `agent_tasks.status='failed'` 条数相等
- [x] 混合：`candidates = failed_direct + pending_self_report`
- [x] 竞态：判死 UPDATE 命中 0 行时 `failed_direct` 不增加、终态不被覆写
- [x] `caplog` 断言日志字段（不止断言任务状态）
- [x] 无候选时不输出汇总行（保留原行为）

## 口径切换点

修复后旧前缀 `marked ... as failed` 只代表真实 DIRECT 失败；24h 内 93 行历史基线需按
`agent_tasks.status` 重判，巡检/日报聚合规则同步改为新三字段。