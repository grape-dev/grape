# Task: Issue #3891

> 一竖切片一提交；测试先行（红→绿）。

- [x] T1 文档：`docs/issue-3891/spec.md` / `plan.md` / `task.md`
- [x] T2 红：`tests/runtime/test_issue_3891_pipeline_shared_resolution.py`
      （7 例——200 空 pipeline、8 编号一致、真不存在 404、不可核实 404、已登记回归 ×2、只读不补建）
- [x] T3 绿：`src/domains/runtime/routes/issues.py`
      - `_normalize_issue_number()` 编号归一单一入口
      - `_sync_missing_issue_row(..., backfill=False)` 只读核实
      - `_resolve_issue_presence()` 统一解析助手（ledger → VCS → 三态）
      - `get_issue_detail` / `get_issue_pipeline` 共用助手
      - `_detail_404` 补 `error_code`
- [x] T3b 同路径存量红测收口：detail 恢复 `synced_by_fallback` 存在性标志
      （`test_issue_667_detail_fallback` 由 2 红转 10 绿）
- [x] T4 既有用例适配：`test_issues_api.py::test_pipeline_not_found` 补 VCS patch（防真实外呼）
- [x] T5 验证：新增 7 例 + `TestIssuePipeline` 2 例 + `test_issue_667_detail_fallback` 10 例全绿；
      基线既有失败集（`TestListIssues*`／`TestIssueEvents::test_events_empty`／arch 棘轮 4 项／
      contracts 5 项）逐项与 HEAD 基线比对一致，零新增失败
- [ ] T6 自审查（5 轴 + 开源前置增量检查：无关内容黑名单 / 敏感信息 pass=0 fail=0）
- [ ] T7 提交 + push + 建 PR（base=`arch-weekly-11-ljt`）+ Issue 评论
- [ ] T8 A2A 通知 QA（场景含 qa 节点才发起）
- [ ] T9 `grape tasks complete`（含 Agent feedback 审视）