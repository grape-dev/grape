# Spec: Issue #667 — issues detail/awaiting-input 404 修复

> 来源：Issue #667 Spec（issue agent 已发布，评级 ready）；本仓实现副本。
> 目标分支：weekly-08-mys；关联 #672（issues detail 404 族，代码侧合并排查不并单）。

## Objective

修复内部需求库对新 Issue 同步/id 映射缺失导致 `issues detail` 与 `issues awaiting-input` 返回 404 的问题：

- 对确认存在的 Issue（VCS 侧可见），`issues detail` 至少一条解析路径可用，不再 404。
- `awaiting-input` 在内部卡片行缺失时具备按 `(会话仓库作用域, issue_number)` 的兜底解析路径。
- 404 语义可区分「Issue 不存在（VCS 侧亦无）」与「需求不存在（内部缺行/同步未落）」。

## Root Cause（时序排查结论）

事件链路：webhook → `run_pipeline`（event_pipeline.py）→ **step 3.2** `ensure_issue_task`
幂等落 `issue` 表行（旁路 best-effort，仅告警）→ agent 入队 → `_inject_issue_pr_env` →
`resolve_issue_card_id` 读行注入 `GRAPE_ISSUE_ID`。

404 发生环：**step 3.2 与「读」之间没有补偿路径**——

1. `run_pipeline` 在 step 3.2 之前有大量提前 return（订阅过滤/配置 level/去重等），新 Issue
   事件被过滤时 **issue 行不落库**，`issues detail`（`_issue_stage_map`/`_task_status_map`/
   events 三处均无）→ 404；
2. step 3.2 本身异常时仅 `log.warning` 旁路吞掉（task-center sync failed），同样缺行；
3. `GRAPE_ISSUE_ID` 注入在行缺失时注入**空串**，下游 awaiting-input 拿空 id → 404「需求不存在」。

## Implementation

1. **兜底解析（detail）**：`GET /api/issues/{owner}/{repo}/{number}` 在三表均未命中时，
   按 repo_key 调 VCS API 核实 Issue 存在性；存在 → `ensure_issue_task` 自动补建内部行并
   正常返回（响应带 `synced_by_fallback: true`）；不存在 → 404 `reason=issue_not_found`；
   VCS 不可核实（无 token/网络失败）→ 404 `reason=internal_sync_missing`（降级语义，保持旧行为可观察）。
2. **兜底解析（awaiting-input）**：`POST /api/issues/{issue_id}/awaiting-input` 在卡片 id 无行时，
   支持新可选字段 `issue_number`：按会话仓库作用域 + number 解析/补建卡片行后继续；
   未提供时 404 detail 带语义区分与指引。
3. **时序修复**：`_inject_issue_pr_env` 注入 `GRAPE_ISSUE_ID` 为空时，先 `ensure_issue_task`
   补建行再重解析（事件本身即 Issue 存在性证据，无需额外 VCS 校验）。
4. **runbook/帮助文档**：`help/api/11-issues.md` 补充 404 reason 语义与降级指引
   （技能包 runbook 更新在 grape-skills 仓跟踪，本仓不重复）。

## Acceptance Criteria

- [x] 复现链路时序结论（见 Root Cause）
- [x] detail 对 VCS 侧存在的 Issue 兜底补建后 200，不再 404
- [x] 404 错误 `reason` 区分 `issue_not_found` / `internal_sync_missing` 并带 suggestion
- [x] awaiting-input 支持 `issue_number` 兜底解析
- [x] help/api 文档更新
- [x] 现有测试通过
- [x] 新增测试覆盖兜底解析与 404 语义区分
