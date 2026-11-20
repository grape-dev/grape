# Plan: Issue #2381 — review-pass 在 admin redispatch 后恒为 noop

## 方案总览
在平台侧做三处收敛，不改变 CLI 契约与 qa 门禁：

1. **末节点判据统一**
   - 新增模块级 `_next_is_last_node(next_value)`：`next in ("done","")` → True。
   - `_is_last_stage_node` 与 `_pipeline_last_node_check` 共用该判据。
   - `_pipeline_last_node_check` 保留「next 指向不存在节点 → 非末节点 + 告警」的防误结单保护。

2. **redispatch 重放保留审核点**
   - `run_pipeline(..., replay: bool = False)` 新增重放标记。
   - `_resume_issue_waiting(..., preserve_review: bool = False)`：为 True 时只恢复
     `awaiting_input`，不动 `pending_review`（审核点保留）。
   - `POST /events/{seq}/redispatch` 的 `run_pipeline` 重放分支传 `replay=True`。
   - 语义：重放的是历史用户评论，不是新的用户输入，不应再次消费审核点。

3. **可区分的 noop 原因码**
   - `_review_noop_reason` 在 `target=completed` 且当前 `processing`、末节点最新事件已
     `completed` 而 `stage` 已回落到非末节点时，返回
     `noop:review_checkpoint_invalidated`；其余维持 `noop:not_pending_review`。
   - 该原因码仅用于降级路径（如历史数据审核点已丢失），提示改用替代登记入口。

## 影响面
| 模块 | 改动 |
|------|------|
| src/domains/runtime/issues_center.py | 公共末节点判据、resume 保留开关、noop 原因码 |
| src/domains/runtime/pipeline/event_pipeline.py | replay 参数透传 |
| src/domains/runtime/routes/events.py | redispatch 重放传 replay=True |
| tests/runtime/test_issue_2381_review_checkpoint.py | 新增覆盖 |

## 风险
- 重放不再恢复 `awaiting_input`：不受影响（仅保留 `pending_review`）。
- `review_checkpoint_invalidated` 误报：仅在末节点事件已 completed 且 stage 已回落时启用，
  且不改变任何状态、只影响原因码字符串。
- 非末节点防护：保持严格 `pending_review` 通道不变。