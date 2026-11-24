# Task: Issue #3771

- [ ] T1（红）新增 `tests/runtime/test_issue_3771_mine_count_domain.py`：域路由 / 降级 / 非分页 / 静态收口四条断言，先在当前分支跑红。
- [ ] T2（绿）`src/domains/runtime/routes/issues.py` 新增 `_count_issues()` 收口，`my_issues` 改走域代理 + `total=None` 降级。
- [ ] T3（绿）`list_repo_issues` 的 COUNT 同点收口，保持失败 `0 + len(pr_items)` 语义。
- [ ] T4 回归：`tests/runtime/test_issue_3771_mine_count_domain.py`、`tests/runtime/test_issues_mine_paging.py`、`tests/runtime/test_mine_filter_behavior.py`、`tests/test_issue_2082_paging.py` 全绿。
- [ ] T5 门禁：`python3 scripts/check-arch.py`、`make lint`（相关文件）无新增告警。
- [ ] T6 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base `arch-weekly-11-ljt`）→ Issue 评论 → A2A QA → `grape tasks complete`。