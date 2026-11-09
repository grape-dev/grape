# Spec: Issue #569 — vod-runbook ISSUE_ID 降级解析字段与 issues detail 实际出参不符

> 来源：Issue Agent 发布的 Spec（评级 ready，标签 bug/P1/spec-ready），本文件为开发侧落档副本。

## Objective
- 修正 vod-runbook Step 4b 的 ISSUE_ID 解析指引：删除 `issues detail | jq -r '.id'` 兜底，改为直接使用服务端注入的 `$GRAPE_ISSUE_ID`，并注明两套 id 体系不可混用。
- 消除 awaiting-input 流程 404「需求不存在」的兜底失败路径，保证文档与 CLI 实际出参契约一致。

## Scope
- In：vod-runbook Step 4b 文本修正（技能资产，位于独立技能仓库 grape-dev/grape-skills）；`issues detail` 出参增加内部 id 字段 `issue_id`（本仓，Spec 可选项）；字段说明标注 number 与内部 id 关系。
- Out：不改 `git-repo issues get` 出参契约；不动 awaiting-input 服务端 404 行为。

## Acceptance Criteria
1. vod-runbook Step 4b 不再出现 `issues detail | jq -r '.id'` 指引
2. runbook 明确 `$GRAPE_ISSUE_ID` 作为 `--issue_id` 唯一来源
3. runbook 注明 `issues detail` 出参不含内部 id、两套 id 不可混用
4. `issues detail` 出参含内部 id 字段 `issue_id` 及说明
5. 所有现有测试通过
