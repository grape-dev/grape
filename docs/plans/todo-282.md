# Todo: Issue #282

- [x] hermes_acp.py 新增 `_global_timeout_for()`（RUNTIME_TASK_TIMEOUT 同源，下限保护）
- [x] 内循环 `_GLOBAL_TIMEOUT` 改用新取值；触发日志带上限值与来源
- [x] 新增 tests/test_hermes_global_timeout.py（值逻辑 5 例 + 行为 3 例）
- [x] 回归 test_runtime_hermes / test_agent_executor / test_executor_silent_extend（既有 3 失败与本改动无关，已 stash 对照确认）
- [x] 提交 feat/issue-282，建 PR → base integration branch
- [ ] QA 审查
