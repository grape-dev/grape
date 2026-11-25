# Tasks: Issue #4340

- [x] S1 定位缺陷点：`executor.py:378` 汇总行使用 `len(stale_tasks)`（候选数）
- [x] S2 新增 `_failed_direct` 计数，DIRECT 判死 UPDATE 按 `rowcount` 累加
- [x] S3 DIRECT UPDATE 补 `status IN ('running','dispatched')` 守卫（0 行不计、不覆写终态）
- [x] S4 汇总行改中性三字段；仅 `failed_direct > 0` 才输出 `marked N stale task(s) as failed`
- [x] S5 单测：REMOTE-only / DIRECT-only / 混合 / 竞态 0 行 / 无候选（`caplog` 断言）
- [x] S6 回归：`test_executor_shutdown.py` / `test_issue_537_pending_self_report.py` / `test_issue_588_pending_since_tz.py` 通过
- [x] S7 门禁：`make lint` 退出码 0、`python3 scripts/check-arch.py` RESULT: PASS
- [x] S8 同口径审计结论（`cleanup.py:852` / `cleanup.py:1016`）随 PR body 说明，不扩大改动
- [x] S9 提交 + push + 建 PR（base `arch-weekly-11-ljt`）

## 既有基线红灯（与本改动无关，改动前即失败，已在 base 复现）

- `tests/runtime/test_shutdown_force_fail.py::TestShutdownForceFail::test_remote_skipped`：
  断言 REMOTE 任务保持 `running`，与 #537 起的 `pending_self_report` 语义冲突。
- `tests/runtime/test_issue_369_failed_event_writeback.py::test_ut_h_recover_orphans_cap_writes_event_diagnostics`：
  `recover_orphans` 判死文案断言（`attempts cap`）与实际文案不符。