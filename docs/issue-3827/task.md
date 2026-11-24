# Task: Issue #3827 — 实施拆解（TDD 红/绿）

> 每个切片：先提交失败测试（`test(#3827)`），再提交最小实现（`fix(#3827)`）。

## S1 · legacy id 池假时钟回归 + 缺列分类兜底

- [ ] 红：`tests/runtime/test_issue_3800_autopilot_offline_backoff.py`
      - `test_legacy_ids_hour_backoff_and_circuit`：legacy id 池 1h exec ≤4、退避 120/240/480、
        熔断告警 1 条、恢复自愈。
      - `test_missing_error_code_column_falls_back_to_text_classification`：`error_code` 未落码
        时仍进入退避（当前实现必然失败）。
- [ ] 绿：`scheduler.py` 加 `_classify_env_error` + `_read_exec_state` 两路统一分类。

## S2 · 结构化日志

- [ ] `_handle_environmental_failure` ERROR 行补 `n`（与 `task_id`/`backoff_until`/`error_code` 对齐）。

## S3 · 收尾

- [ ] `pytest tests/runtime/test_issue_3800_autopilot_offline_backoff.py` 全绿。
- [ ] 相关回归：`tests/runtime`、`tests/mgmt`。
- [ ] PR 描述 + Issue 评论 + A2A QA；运行时验证（主控重启/生产实查）由 sre 执行。