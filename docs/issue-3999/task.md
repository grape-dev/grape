# Issue #3999 — 任务拆解

- [ ] T1 `src/kernel/gsb/issue_card_metrics.py`：`INVALID_IDENT_METRIC` + `install` + `provider_of` + `observe_invalid_ident`
- [ ] T2 `src/domains/insight/observability/__init__.py`：注册 `issue_card_metrics`
- [ ] T3 `src/api/session_store.py`：`get_issue_stage` invalid-ident 计数 + provider/回退 warning
- [ ] T4 `tests/runtime/test_issue_3999_stage_ident_observability.py`：计数器 / warning / 回退 / 数字零回归
- [ ] T5 定向回归：`test_issue_4065_issue_card_ident` / `test_issue_state` / `test_event_conversation`
- [ ] T6 `make lint` + `python3 scripts/check-arch.py`
- [ ] T7 提交 / push / 建 PR / Issue 评论 / A2A QA / `grape tasks complete`