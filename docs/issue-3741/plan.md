# Plan: Issue #3741

## 改动清单

| 文件 | 改动 |
|------|------|
| `src/domains/runtime/engine/task_timeout.py` | 新增 `DEFAULT_NO_PROGRESS_REPEAT_STEPS=8`、`RUNTIME_NO_PROGRESS_REPEAT_STEPS` 与 `no_progress_repeat_steps(task, cfg)` 配置链；新增 `DEFAULT_NO_PROGRESS_REPEAT_WINDOW_SEC=0` / `RUNTIME_NO_PROGRESS_REPEAT_WINDOW` / `REPEAT_STEP_BUDGET_SECONDS=120` 与 `no_progress_repeat_window_sec(task, cfg)`（窗口与阈值解耦） |
| `src/domains/runtime/engine/service.py` | `update_task_status` 新增可选 `expect_started_at` 乐观条件（rowcount=0 即放弃终态写入） |
| `configs/task-timeout.example.json` | 补 `no_progress_repeat_window_sec` 与 `no_progress_retries=0` 的新语义说明 |
| `.github/workflows/lint.yml` | 新增 #3741 回归套件 CI 白名单入口（与本单测试段同一组命令） |
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

- 误杀：判据只用「连续相同指纹」，不含时长；`no_progress_repeat_steps=0` 单独关闭指纹判据
  （只保留 #2793 活跃度判据），`no_progress_repeat_window_sec` 可显式收窄/放宽参与窗口。
- 整体回滚：`no_progress_enabled=false`（任务 env > per_skill > per_repo > 顶层配置 > 进程 env）
  只留诊断、不动任务，**并同时关闭本单新增的 poll 超时收口**
  （`_poll_dispatched_completion` 超时分支）**与 failed exec 的反向回收**
  （`_reap_terminal_exec_running_tasks` 的 failed 分支）——两者先过
  `_no_progress_termination_enabled` 门禁，关闭时只写 exec failed / 日志。
  用户取消（cancelled）的传播不受该开关约束：「exec 已取消但任务仍活跃」本身就是
  本单要治的残留形态。
- `no_progress_retries=0` = 不重派、命中即收口 `failed(reason=no_progress)`（不是「只诊断」；
  「只诊断」由 `no_progress_enabled=false` 承担），文档与实现同口径。
- 收口竞争：所有终止写入都带 `started_at=?` 乐观条件（`update_task_status(expect_started_at=...)`），
  rowcount=0 即放弃；`_terminate_stuck_agent_task` 与 `_finalize_no_progress` 均按此口径收口，
  重派复位 `started_at` 后不会误杀新一轮执行。
- 反向 sweep 误伤（PR #2160 复审 Required）：`agent_task_id` 与 `autopilot_executions`
  是 1:N（重派回炉会把同一个 task 重新 claim），因此 ①只取该任务**最新**一条 exec
  （`e.id = (SELECT MAX(id) ... WHERE agent_task_id = e.agent_task_id)`）；
  ②`queued` 且 `started_at` 为空（已回炉未 claim）从活跃集排除；③用
  `_exec_and_task_same_round()` 按时间区间判定 exec 与任务当前执行同期
  （`agent_tasks.started_at` 是 claim 时刻、`exec.started_at` 是派发时刻，两者不做相等比较，
  5s 容差只吸收落库间隔），并留 60s 宽限；失败指标带 JOIN 出的 `runtime_id`。
- poll 超时同轮判据（同上复审）：轮询期间记住首次观察到的任务 `started_at`，超时时
  与当前值比较——不一致说明任务已被回炉重派，只写 exec failed、不终止新一轮执行。
