# Plan: Issue #729 — duplicate_a2a 拦截携带原任务产物引用

## 改动点
1. `src/api/routes/webhook.py`
   - 新增 `_duplicate_a2a_original_refs(repo_key, existing_seq)`：回查原事件
     （events 表）+ 原任务引用（复用 `pipeline_status.load_segment_task_refs`，
     与流水线 segments[].output.task_ref/result_ref 同一套构造逻辑）。
   - 新增 `_duplicate_a2a_skip_response(...)`：同 issue 同 agent 去重拦截响应体，
     新增 `original_event` 字段（向后兼容）。
   - 跨流水线去重（`_cross_pipeline_handoff` duplicate_handoff 分支）返回值与
     `a2a_endpoint` 拦截响应同步透传 `original_event`。
2. `docs/issue-729/cross-pipeline-artifact-continuation.md`：runbook 章节
   （跨流水线续接产物标准路径，两场景示例）。
3. `tests/test_issue_729_duplicate_a2a_refs.py`：引用字段测试（available /
   pending 边界 / unavailable / 事件缺失；两拦截分支响应体）。

## 关键决策
- 透传「引用」不拉「产物内容」：`task_ref.query_path`（授权轮询）+ `result_ref`
  （pending / available+脱敏摘要 / unavailable），与 #539 pipeline 引用语义一致；
- result pending 边界：原任务在途时 `result_ref.status=pending`，仅 `task_ref`
  可轮询，不构造摘要；
- 查询失败/无关联任务 → 显式 unavailable，不阻断拦截响应（拦截语义不变）。

## 风险
- 与 #665 交叉：仅新增响应字段，不改拦截判定与通知语义，冲突面最小。
