# Task: Issue #3741

- [x] T1 `task_timeout.no_progress_repeat_steps` 配置链（默认 8，0 关闭）
- [x] T2 `task_activity.repeated_tool_steps` 指纹判据
- [x] T3 `_sweep_no_progress_tasks` 接入指纹判据并复用 #2793 重派语义
- [x] T4 `_finalize_no_progress` 重派耗尽收口（failed + 释放槽位 + 事件/exec 同步）
- [x] T5 `_poll_dispatched_completion` 超时先终止 agent_task 再写 exec failed
- [x] T6 `_reap_terminal_exec_running_tasks` 反向残留 sweep + 指标
- [x] T7 新增回归用例 `tests/runtime/test_issue_3741_no_progress_stall.py`
- [x] T8 自审查 / lint / arch check
