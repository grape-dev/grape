# Task: Issue #3800 — 实施拆解（TDD 红/绿）

> 每个切片：先提交失败测试（`test(#3800)`），再提交最小实现（`feat(#3800)`）。

## S1 · 错误码与退避计算（纯函数 seam）

- [ ] 红：`tests/runtime/test_issue_3800_autopilot_offline_backoff.py::test_compute_backoff_sequence`
      断言 `_compute_backoff_next_run('*/1 * * * *', n)` 为 60/120/240/480/960/1800/1800 秒；
      `ERROR_ENGINE_OFFLINE` / `ERROR_BINDING_STALE` 常量存在。
- [ ] 绿：`scheduler.py` 加常量 + `_scheduler_now` + `_cron_period_seconds` + `_compute_backoff_next_run`。

## S2 · 绑定探测 seam

- [ ] 红：`test_probe_task_binding_states` —— uid 存在但离线 → `ENGINE_OFFLINE` 判据；
      uid 查无此行 → `BINDING_STALE` 判据；无绑定 → `mode=""`。
- [ ] 绿：`_probe_task_binding(task)`，并让 `_execute_autopilot_remote` 两条路径复用，
      写 `error_code`（`_update_execution_result` 增 `error_code` 形参，缺列时降级）。

## S3 · DDL + 跨域写原语

- [ ] 红：`test_failure_state_columns_and_migration` —— 两张表的列在位、迁移登记可判定、幂等。
- [ ] 绿：两份 schema DDL、两条登记式迁移、mgmt 写/清原语（`_ANY_QUERIES` + lib + `_any`）、
      `route_config.PUBLIC_PRIMITIVES["mgmt"]` 同步、读侧列序追加。

## S4 · 收口裁决 + 假时钟 1h 回归

- [ ] 红：`test_engine_offline_hour_backoff_and_circuit`（exec ≤ 4、退避序列、告警 1 次、
      `circuit_paused=1`）；`test_engine_recovered_auto_resume`；`test_binding_stale_first_dispatch_alerts`。
- [ ] 绿：`_execute_task` 环境性失败分支 + `_handle_environmental_failure` +
      `_scan_task_once` 闸门 + `_alert_task_failure` + 终态结构化 ERROR 行。

## S5 · 收尾

- [ ] 全量/相关测试回归（`tests/runtime`, `tests/mgmt`, `tests/arch`, `tests/api/test_migration_track_guard.py`,
      `tests/kernel/test_gsb_route.py`）。
- [ ] PR 描述（改动/测试/回滚）+ Issue 评论 + A2A QA。