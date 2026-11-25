# Plan: Issue #3741

## 改动清单

| 文件 | 改动 |
|------|------|
| `src/domains/runtime/engine/task_timeout.py` | 新增 `DEFAULT_NO_PROGRESS_REPEAT_STEPS=8`、`RUNTIME_NO_PROGRESS_REPEAT_STEPS` 与 `no_progress_repeat_steps(task, cfg)` 配置链 |
| `src/domains/runtime/engine/task_activity.py` | 新增 `repeated_tool_steps()`：tool+input 指纹去重与连续计数，返回诊断（步号区间/指纹/工具名） |
| `src/domains/runtime/engine/scheduler.py` | `_sweep_no_progress_tasks` 接入指纹判据；新增 `_finalize_no_progress`（failed 收口）、`_terminate_stuck_agent_task`（超时/反向残留终止）、`_reap_terminal_exec_running_tasks`（反向 sweep）；`_poll_dispatched_completion` 超时先终止任务 |
| `tests/runtime/test_issue_3741_no_progress_stall.py` | 新增：指纹判据、耗尽收口、poll 超时终止、反向 sweep 回归 |
| `tests/runtime/test_issue_2793_no_progress.py` | 若耗尽语义调整，补齐既有断言 |

## 执行顺序

1. 配置链 → 纯函数判据 → 单测（RED）。
2. 巡检接入 + 收口（GREEN）。
3. poll 超时 + 反向 sweep + 单测。
4. 自审查（5 轴 + 增量敏感扫描）、lint、arch check。
5. 建 PR → Issue 评论 → A2A QA。

## 风险与回滚

- 误杀：判据只用「连续相同指纹」，不含时长；`no_progress_enabled=false` 与
  `no_progress_repeat_steps=0` 均可整体/单项回滚。
- 收口竞争：所有状态更新都带 `started_at=?` 乐观条件，rowcount=0 即放弃，避免覆盖新执行。
- 反向 sweep 误伤：仅匹配 `agent_task_id` 非空、exec 已终态且任务仍活跃的行，并留 60s 宽限。
