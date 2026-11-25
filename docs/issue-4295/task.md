# Issue #4295 任务拆解（Task）

| # | 任务 | 交付物 | 验证 | 状态 |
|---|---|---|---|---|
| 1 | 有界等锁原语 | `db_lock_metrics.LockAcquireTimeout` / `acquire_budget_s()` / `foreground_lock_budget()`（`None`=继承、`0`=关闭）；`TrackedRLock.acquire` 有界分支；超时 ERROR 按 owner 去重（`DB_LOCK_TIMEOUT_LOG_DEDUP_S`） | `tests/kernel/test_issue_4295_db_lock_budget.py`（15 例） | 完成 |
| 2 | 持有看门狗 | `db_lock_metrics.start_watchdog/stop_watchdog/watchdog_scan/_holder_stack/reset_after_fork`；`snapshot()` 补 `tid`；去重表加锁 | `tests/kernel/test_issue_4295_db_lock_watchdog.py`（10 例） | 完成 |
| 3 | 看门狗随锁就位 | `session_manager.py` 导入期启动（幂等、daemon、`DB_LOCK_WATCHDOG_S=0` 可关）+ `os.register_at_fork(after_in_child=...)` 在每个 fork 子进程重启（preload 形态） | `test_watchdog_thread_starts_with_session_manager_and_is_idempotent` / `test_watchdog_restarts_in_fork_child_after_preload` | 完成 |
| 4 | 超时语义 503 | `src/api/exception_handlers.py` 新增 `LockAcquireTimeout` 处理器（503 + `Retry-After` + `request_id`，ERROR 带 owner/holder） | `tests/api/test_issue_4295_lock_timeout_503.py`（6 例） | 完成 |
| 5 | 锁内外部 IO 清出 | `issues_center.close_issue_tasks` / `_close_issue_tasks_bot_takeover` 通知投递移出 `with db_lock`（状态判定/UPDATE/commit 仍在锁内） | `tests/kernel/test_issue_4295_external_io_gate.py::test_review_point_notify_is_dispatched_outside_db_lock`（参数化 2 例）+ 反向断言 UPDATE 仍在锁内 | 完成 |
| 6 | 防回归门禁 | `scripts/check_db_lock_external_io.py`（AST，棘轮，`--list`/`--update-baseline`；覆盖 `with db_lock:` 与 `acquire()+try/finally` 两种形态）+ `Makefile lint` | `test_gate_script_reports_no_violation_on_src` / `test_gate_detects_synthetic_violation` / `test_gate_ignores_local_only_and_plain_db_calls` / `test_gate_does_not_treat_user_level_threading_lock_as_db_lock` / `test_gate_detects_violation_in_acquire_release_block` / `test_gate_acquire_release_ignores_pure_db_block` / `test_gate_acquire_release_requires_the_registered_lock` | 完成 |
| 7 | 并发验收用例 | 3 写 + 1 读：免锁读通道不被锁串行化、前台读有界等锁、线程归因断言可失败 | `tests/kernel/test_issue_4295_concurrent_read_write.py`（5 例） | 完成 |
| 8 | 运维可调/可回滚 | `.env.example` 6 个 env（预算/Retry-After/看门狗周期/阈值/去重/超时日志去重） | 文档 | 完成 |
| 9 | 回归验证 | `tests/kernel`（1031 passed / 53 failed＝基线同数）、`tests/kernel/test_db_lock_metrics.py`、`make lint` | 见 PR 描述 | 完成 |

## 交付前检查（pre-pr-release-check）

| 检查 | 结果 |
|---|---|
| 无关内容黑名单（运维/临时脚本、日志、真实配置） | 见 PR 描述实际数字 |
| 敏感信息增量扫描（`origin/arch-weekly-11-ljt...HEAD`，AK/SK、token） | 见 PR 描述实际数字 |
| commit message 全英文 | 是 |
| 仅本次改动范围（未全历史扫描/未重写历史） | 是 |

## 后续（不在本 PR）

- 上线后 1h 窗口验收：`grape_db_lock_hold_seconds` 分桶、`grape_db_lock_watchdog_events_total{owner}`、
  `grape_db_lock_acquire_timeouts_total{owner}`、`db_lock watchdog` ERROR 与 `Retry-After` 503 计数；
- 若仍有长持有：按 `watchdog` 抓到的持锁栈逐点处理（分域锁/读写锁改造另立 Issue）。