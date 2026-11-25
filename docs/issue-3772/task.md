# Tasks: Issue #3772

- [ ] T1（红）`tests/kernel/test_mysql_compat.py` 追加双后端迭代/上下文一致性用例，
      当前实现应失败。
- [ ] T2（绿）`MySQLCompatCursor` 补 `__iter__`/`__next__`/`__enter__`/`__exit__`。
- [ ] T3（红）`tests/runtime/test_issue_3772_no_progress_scan.py` 构造非可迭代 cursor，
      证明扫描调用点仍依赖隐式迭代（当前失败）。
- [ ] T4（绿）`_sweep_no_progress_tasks()` 改 `execute` + `fetchall`。
- [ ] T5 指标：insight observability 新增 `grape_no_progress_scan_failures_total` +
      public 转发 + scheduler 失败计数/连续 5 次 ERROR。
- [ ] T6 告警：`deploy/alerts.yml` 新增 no-progress 扫描失败规则。
- [ ] T7 全仓核验 `list(<cursor>.execute(...))` / `for row in cursor` 同类写法。
- [ ] T8 回归：`pytest tests/kernel/test_mysql_compat.py tests/runtime/test_issue_2793_no_progress.py tests/runtime/test_issue_3772_no_progress_scan.py`。
- [ ] T9 自审查（5 轴 + pre-pr 增量检查）、建 PR、Issue 评论、A2A QA、tasks complete。