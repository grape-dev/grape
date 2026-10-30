# Plan: Issue #539 — 实现方案

## 数据流
```
issues pipeline 端点（routes/issues.py）
  ├─ 旧逻辑不变：_pipeline_from_events / _pipeline_from_archive → {issue, summary, status, segments}
  ├─ pipeline_status.load_issue_business_status(repo_keys, number)   ← issue 表（stage/status）+ issue_state 兜底
  ├─ pipeline_status.load_segment_task_refs(repo_keys)               ← agent_tasks（event_seq → task_ref/result_ref）
  └─ pipeline_status.enrich_pipeline_business_status(pipeline, ...)  ← 就地叠加新增可选字段
```

## 字段设计
- 顶层新增（可选）：`issue_stage`、`issue_task_status`、`review_status`
  （枚举：pending_review / completed / cancelled / awaiting_input / in_progress / unknown）；
- `segments[].output` 新增（可选）：
  - `task_ref`：`{task_id, task_status, query_path}`，query_path → `GET /api/runtime/tasks/{task_id}/status`
    （授权查询全量 result_text 的路径）；无关联任务 → `{task_id: null, task_status: "unavailable", query_path: ""}`；
  - `result_ref`：`{status}`，枚举 `available`（+脱敏 `summary`，≤240 字）/ `pending`（任务在途）/
    `unavailable`（无结果或不可读）；
- `issues_pipeline_detail`（按 issue.id，详情页卡片）同步新增 `issue_stage` / `issue_task_status` /
  `review_status` / `sre_result`（SRE 最新任务的 task_ref+result_ref）。

## 摘要与脱敏（pipeline_status.extract_result_summary）
- result_text 为结构化 JSON（tasks complete 组装）时优先取 conclusion → issue_summary → result_text；
- 脱敏：`tk_ / grape_ / rta_ / gh[pousr]_ / AK / SK` 前缀长 token → `[REDACTED]`；
- 压缩空白、限长 240 字；仅引用+摘要，绝不内嵌全量 result_text / errors / stderr。

## 端点改动
| 端点 | 改动 |
|------|------|
| GET /api/issues/{owner}/{repo}/{number}/pipeline | 叠加顶层业务状态 + 各段 task_ref/result_ref（旧字段不变） |
| GET /api/issues/{owner}/{repo}/{number}（detail） | 内嵌 pipeline 同步叠加 |
| GET /api/issues/{issue_id}/pipeline（详情页卡片） | 新增 review_status 等字段 + sre_result |
| GET /api/runtime/tasks/{task_id}/status | 仅描述澄清：task_ref.query_path 的授权查询路径（只读关联） |

## 前端（src/api/static/js/pages/issues.js _renderPipelineSection）
- `review_status === 'pending_review'` → 橙色横幅「需求待人工确认：阶段执行完成 ≠ 需求/生产发布完成」；
- `sre_result` → 「SRE 验收结论」块：available 显摘要、pending/ unavailable 显式文案 + 任务引用行。

## 风险与缓解
- 汇总层跨表取数延迟 → 单次批量 SQL（IN 查询），失败降级为显式 unavailable，不阻塞；
- result_text 敏感泄漏 → 正则脱敏 + 限长 + 仅摘要，不透出 errors/stderr/全量文本。
