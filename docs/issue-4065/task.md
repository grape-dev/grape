# Issue #4065 — 任务拆解（task）

| # | 切片 | 改动 | 验收 | 提交 |
|---|------|------|------|------|
| T1 | 归一口径单点 | `src/kernel/issue_ident.py`：`deliverable_comment_ident` / `normalize_card_ident` / `card_ident_number` | 三类 ident（`autopilot-*`、`IK*`、`pr_*`）矩阵全绿 | 红→绿 |
| T2 | 投递与预检短路 | `event_pipeline._post_issue_comment_ledgered` / `_list_issue_comments_card` 归一后短路，非数字零远程请求 | monkeypatch `create_client` 断言未发 `_request`；日志出现 `RESULT_COMMENT_SKIPPED` | 红→绿 |
| T3 | 结果路由 + 真实号双保险 | `_post_result_to_issue` 前置归一、`_task_real_issue_number`、`_record_result_card_fallback` | 有效号投递成功；无有效号落 `RESULT_CARD_FALLBACK` 且任务行可读 | 红→绿 |
| T4 | 指标与告警 | `src/kernel/comment_metrics.py`、observability 注册、`deploy/alerts.yml` | `grape_comment_post_failed_total{reason}` 可聚合；连续 N 次出 `RESULT_COMMENT_ALERT` | 红→绿 |
| T5 | 卡片端点归一 | `routes/internal.py` 签名 str + 可读 400；`session_store._issue_num_or_ident` 走 `card_ident_number` | `autopilot-*`/`IK*` 不再 422；`pr_*` 400 + 可读 message | 红→绿 |
| T6 | 回归 | `tests/runtime/test_issue_4065_*`、`tests/api/test_issue_4065_*` | `pytest tests/runtime tests/api tests/kernel tests/arch` 无新增失败 | — |

## 执行顺序

T1 → T2 → T3 → T4 → T5 → T6（严格按序；每个切片先红后绿，红提交证明契约存在）。

## 完成定义

- 六条 Acceptance Criteria 全部有对应测试断言；
- 现有测试无新增失败（基线既有失败另列）；
- 自审查（5 轴 + pre-pr 增量检查）通过后建 PR。