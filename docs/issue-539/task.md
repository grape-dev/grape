# Task: Issue #539 — 任务拆解与执行记录

| # | 任务 | 状态 |
|---|------|------|
| 1 | pipeline_status.py：review_status 枚举 + derive_review_status / redact_sensitive / extract_result_summary / build_task_ref / build_result_ref / load_issue_business_status / load_segment_task_refs / enrich_pipeline_business_status | ✅ |
| 2 | routes/issues.py：issues_pipeline（路线图）与 issues_detail 内嵌 pipeline 叠加业务状态与段引用 | ✅ |
| 3 | routes/issues.py：issues_pipeline_detail（按 issue.id）新增 issue_stage/issue_task_status/review_status/sre_result | ✅ |
| 4 | routes/runtime.py：runtime_task_status 描述澄清（task_ref.query_path 的授权查询路径，只读关联） | ✅ |
| 5 | 前端 issues.js：_renderPipelineSection 增加待人工确认横幅 + SRE 验收结论块 | ✅ |
| 6 | help/api/11-issues.md：status 语义澄清 + 新字段文档（路线图与详情卡两处） | ✅ |
| 7 | 回归测试 tests/test_issue_539_pipeline_review_status.py：5 类场景 + 兼容性 + 脱敏（21 用例） | ✅ |
| 8 | 基线对照验证：HEAD（weekly-09-ljt）上既存的 13 个测试失败与本次改动无关（git archive 对照运行） | ✅ |

## 验证
- `python3 -m pytest tests/test_issue_539_pipeline_review_status.py` → 21 passed；
- 关联回归：test_issues_api / test_issue_pending_review / test_pipeline / test_pipeline_terminal_status /
  test_issue_89 → 与基线一致的通过集（无新增失败）；
- `node --check src/api/static/js/pages/issues.js` → OK。
