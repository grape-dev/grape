# Tasks: Issue #2381 — review-pass 在 admin redispatch 后恒为 noop

- [ ] T1 `_next_is_last_node` 公共判据 + 两个调用方统一（issues_center.py）
- [ ] T2 `_resume_issue_waiting(preserve_review=...)` 保留 pending_review
- [ ] T3 `run_pipeline(replay=...)` / redispatch 重放分支透传
- [ ] T4 `noop:review_checkpoint_invalidated` 原因码与检测
- [ ] T5 单测：末节点四态（done/空串/未知/不存在节点）、resume 保留、noop 原因码
- [ ] T6 回归：`tests/runtime/test_issue_pending_review.py`、`test_issue_656_auto_finalize.py`
- [ ] T7 自审查 + 敏感信息增量扫描 + 建 PR