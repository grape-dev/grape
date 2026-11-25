# Task: Issue #4170 — 交付终态终态抑制

- [x] 终态判定单点函数 `is_issue_delivery_terminal`（merged + closed，fail-open）
- [x] `events` 查询承载 PR merged 证据（兼容 `#N` / `N`）
- [x] VCS 读 Issue 仓库态 closed
- [x] `run_pipeline` issue_comment 终态守卫（仅记录、不派发，早于等待态恢复）
- [x] `event_store.append_terminal_suppressed_event`（status=skipped + event_skipped 审计 + 评论幂等键）
- [x] `_resume_issue_waiting` 终态复位抑制
- [x] 事件留痕以 `agent_type=""` 落库，节点进度聚合天然忽略（刷新基线无 dedup 留痕 helper，原 docstring 同步随基线架构移除）
- [x] 单测：终态抑制（0 派发/0 复位/事件可查）+ 非终态回归 + 双条件/reopen/fail-open
- [x] 文档：spec / plan / task

## 验证命令
```bash
python3 -m pytest tests/runtime/test_issue_4170_terminal_suppression.py -q
```