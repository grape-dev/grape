# Plan: Issue #426 — PR 订阅与 stage matrix 路由并行共存

## 方案
PR 订阅路由从「PR 关联 Issue 时整体放弃」改为「与阶段矩阵（stage matrix）路由并行共存」：

1. **`_filter_pr_sub_matches` 重构**：
   - 移除原步骤 1（无条件排除 Issue 所在场景）与步骤 2（排除同 qa_skill 场景）。
   - 新增 `skip_issue_scene` 参数：仅当主路由（stage matrix）已为该事件派发 QA 时排除 Issue 所在场景（避免同场景重复 QA，保留原设计意图）。
   - 保留步骤 3：同 qa_skill 场景去重（#318 成果）。
   - `issue_qa_skill` 参数随步骤 2 一并移除。

2. **主路由门控**（原 `if not agent_type and event_type in PR_EVENT_TYPES`）：
   - 改为 `if event_type in PR_EVENT_TYPES`（PR 订阅匹配始终执行）。
   - `agent_type` 为空：保持原行为——首个匹配作为主 agent（qa），其余场景并行补派。
   - `agent_type` 非空（PR 关联 Issue，stage matrix 已设定）：全部匹配通过 `_dispatch_pr_subscription_extra` 并行派发（a2a_event），与主路由 agent 互不干扰。
   - 过滤条件 `skip_issue_scene = (agent_type == "qa")`。

3. **1.5 预匹配（bot actor）**：
   - 同步移除无条件排除；通过 `_resolve_issue_stage_agent` 判断主路由是否将派发 QA，仅在此时跳过同场景。
   - bot actor 行为与其余路径口径一致。

## 不变量
- 单场景、无绑定、人类用户：仍恰 1 条主事件，无 a2a_event（#318 双重触发禁令）。
- bot actor：仍只走 1.5 预派发，主路由被自循环检测拦截。
- 同 qa_skill 多场景去重保留。

## 影响面
- `src/api/event_pipeline.py`：`_filter_pr_sub_matches`、1.5 预匹配、主路由门控三处。
- `tests/test_issue_426_pr_sub_binding.py`（新增）。
