# Task: Issue #1105

- [ ] test: update_event_status completed/cancelled 终态写 completed_at（红）
- [ ] test: update_event_complete 空 completed_at 兜底（红）
- [ ] test: admin-confirm 收敛分支补 completed_at（红）
- [ ] feat: event_store.py 两处收口
- [ ] feat: routes/issues.py admin-confirm 收口
- [ ] migration: backfill_events_terminal_completed_at（幂等分批回填）
- [ ] 回归：test_event_status_protection / test_issue_476 / test_event_redispatch 等相关用例
