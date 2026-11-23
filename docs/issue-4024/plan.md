# Plan: Issue #4024 — 审核点态读回口径统一

## 设计要点

采用 Issue Spec 的设计决策 **B**：不改变 `status` 任务态枚举语义，新增独立审核点态
字段 `review_status`，置位与读回共用同一派生函数与同一事实源。

## 改动点

| 文件 | 改动 |
|------|------|
| `src/domains/runtime/routes/issues.py` | `get_issue_status` 读取 `stage` 并输出 `review_status`；`issue_pending_review` 回读落库真值并输出 `review_status` + `idempotent`；新增 `_issue_stage_status_by_id`，`get_issue_detail` 已知内部主键时以该行为单一事实源 |
| `tests/runtime/test_issue_4024_review_status_readback.py` | 新增置位→立即读回、pipeline/detail 去滞留、双 runtime 对账、聚合口径契约用例 |
| `docs/openapi/11-issues.md` | status get 出参补 `stage`/`review_status` 并注明聚合口径来源；新增 pending-review 响应字段说明 |
| `docs/issue-4024/*.md` | 需求/方案/任务文档 |

## 读回同源化

```
issue (id) ── status + stage ──► derive_review_status ──► review_status
   │                                                        ▲
   ├─ issues pending-review（写：status=pending_review）      │
   ├─ issues status get（读：同 id 行）──────────────────────┤
   └─ issues detail / pipeline（读：同 id 行，已知主键时）────┘
```

`get_issue_status`、`issue_pending_review`、`get_issue_detail` 均以 `issue.id`
定位同一行，避免 `repo_key + issue_number` 多别名行聚合顺序不一致造成的口径分歧。

## 兼容性

- `status` 字段语义与枚举不变；`review_status` 为新增可选字段，向后兼容。
- `pending-review` 响应新增 `review_status`，`status` 改为回读真值：成功置位仍为
  `pending_review`；终态/幂等场景回读真实状态，并携带 `idempotent` 标识（对齐既有
  端点描述契约）。
- 无 schema/字段删除，OpenAPI 端点集合不变。

## 验证

- `pytest tests/runtime/test_issue_4024_review_status_readback.py`
- `pytest tests/runtime/test_issue_539_pipeline_review_status.py`
- `pytest tests/test_issue_2695_pending_review_invalid_id.py`