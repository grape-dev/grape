# Spec: Issue #729 — 跨流水线续接产物标准路径（runbook 章节）

> Coding Agent | 基线分支: weekly-08-mys（原 Spec weekly-09 已按移交确认订正）

## Objective
为技能 runbook 补充「跨流水线续接产物标准路径」章节：duplicate_a2a 拦截响应携带
`original_event.task_ref / result_ref / query_path` 后，下游 Agent 如何凭引用定位并续接
原任务产物。覆盖重复交办与跨流水线两场景，含示例。

## Scope
- In：runbook 文档章节 + 两场景示例（本文档，`docs/issue-729/cross-pipeline-artifact-continuation.md`）。
- Out：产物内容获取 API 本体（#650）；重复事件通知已运行 coding 的语义修复（#665）。

## Acceptance
- [ ] 章节含重复交办 / 跨流水线两场景示例
- [ ] 示例引用字段与拦截响应实现一致（task_ref / result_ref / query_path / pending 边界）
