# Spec: Issue #426 — PR 订阅场景在 PR 与 Issue 绑定关系时彻底失效

> 来源：issue agent 已发布 Spec（评级 ready）；本文为仓库内落档副本。

## 目标
修复 PR 订阅场景在 PR 与 Issue 存在绑定关系时被整体放弃的缺陷，使订阅场景在绑定关系下仍正常派发与执行。

## 根因
1. `_filter_pr_sub_matches`（event_pipeline.py）：PR 关联 Issue 时排除「Issue 所在场景」+「同 qa_skill 场景」，导致订阅场景被清空。
2. 主路由门控 `if not agent_type`：PR 关联 Issue 时 stage matrix 已设 agent_type，PR 订阅匹配分支整体不可达。
3. 1.5 预匹配同步调用 `_filter_pr_sub_matches`，bot actor 路径同样被排除。

## 范围
- In：`src/api/event_pipeline.py` 三处路由逻辑修复 + 回归测试。
- Out：#427 env 注入层缺陷（独立修复）；#318 去重逻辑（保留）；stage matrix 本身（不动）。

## 验收标准
- PR 关联 Issue 且 Issue stage≠qa：PR 订阅场景 QA 正常派发（含与 Issue 同场景的订阅场景）。
- PR 关联 Issue 且 stage matrix 已派发 QA（agent_type=qa）：仅跳过同场景订阅 QA，其余场景并行派发。
- 无绑定 PR 订阅行为不回归（#318/#368 测试全绿）。
- 同 qa_skill 多场景去重保留。
