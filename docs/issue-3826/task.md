# Task: Issue #3826

- [ ] T1（红）新增 `tests/runtime/test_issue_3826_count_no_fallback.py`：三库拓扑缺 DSN 不回落 +
      `DomainUnavailable` 可辨识 + 日志 `route/target_db/pid/host` + `_DomainDbProxy` 棘轮 +
      全仓 COUNT 审计守门；同提交把 `test_issue_3771_mine_count_domain.py` 的拓扑 seam 迁到
      `route_of` 依赖面。先在当前分支跑红。
- [ ] T2（绿）`src/kernel/db_registry.py` 新增 `database_of(domain)`，`expected_db_name` 复用。
- [ ] T3（绿）`src/domains/runtime/routes/issues.py`：`_DomainDbProxy` 走 `route_of`（删回落分支），
      `_count_failure_context()` + `_count_issues` 日志增强 → T1 转绿。
- [ ] T4 门禁：`.github/workflows/windows-smoke.yml` 的 #3771 步骤 `run` 追加 #3826 用例文件。
- [ ] T5 回归：`python3 -m pytest tests/runtime/test_issue_3826_count_no_fallback.py
      tests/runtime/test_issue_3771_mine_count_domain.py tests/runtime/test_domain_proxy_commit_2524.py
      tests/runtime/test_report_rollback_2524.py tests/runtime/test_pipeline_title_backfill_2925.py
      tests/runtime/test_pipeline_window_derived_alias.py tests/kernel/test_domain_route_strict.py
      tests/kernel/test_domain_db.py tests/runtime/test_issues_mine_paging.py -q` 全绿。
- [ ] T6 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base `arch-weekly-11-ljt`）→ Issue 评论 →
      A2A QA → `grape tasks complete`。
- [ ] T7（sre，非本期编码）4 台主控逐台 host+pid+启动时间+提交指纹核对，1 小时 0 条/小时回帖。