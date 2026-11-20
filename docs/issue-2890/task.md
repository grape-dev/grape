# Issue #2890 任务拆解

- [x] T1 在 `branch_guard.py` 引入异常收集器 `_guard_issue_sink` + `_guard_stage_failed`
- [x] T2 改写 `load_lease_state` / `save_lease_state` / `fetch_pr_state` /
      `_fetch_pr_state_via_grape` 的单点告警
- [x] T3 `hook_main` 聚合输出唯一一条结构化告警，聚合自身 try/except 兜底
- [x] T4 新增 `tests/test_issue_2890_branch_guard_aggregate_warning.py`：
      缺表场景断言 stderr 告警 JSON 仅 1 条、`fail_open=true`、退出码 0
- [x] T5 回归执行 branch_guard 相关测试（#993 / #2028 / #2890）全通过