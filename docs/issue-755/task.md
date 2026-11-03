# Task: Issue #755

- [x] T1 helper 三件套：`_issue_exists_in_db` / `_resolve_cross_repo_issue_number` / `_a2a_issue_vcs_check`
- [x] T2 `a2a_endpoint` 入口回填 + 鉴权后 VCS 存在性校验（400）
- [x] T3 `_cross_pipeline_handoff` 回填 + carried_context.target_issue_number 持久化
- [x] T4 `resolve_issue_id` 防御性 target 映射
- [x] T5 新增测试 `tests/test_issue_755_a2a_issue_number.py`
- [x] T6 回归：`test_issue_329_cross_pipeline.py` / `test_issue_671_a2a_repo_fallback.py` / 相关 a2a 测试
- [x] T7 自审查（5 轴 + pre-pr 增量检查）→ commit → push → PR（base=weekly-08-mys）→ Issue 评论 → A2A QA → tasks complete
