# Issue #679 任务拆解（task）

- [x] T1 定位三处占位发帖入口（event_pipeline.py×2 / service.py×1）并核对 service.py:2018 补偿状态描述不动
- [x] T2 `_post_result_to_issue`：else 分支改 return 跳过
- [x] T3 `_post_result_to_pr`：无 conclusion 时回填纯文本，仍无则 return 跳过
- [x] T4 `complete_task`：`_result_text` 空时跳过 `post_issue_comment`
- [x] T5 修正 `test_issue_451_placeholder_comment.py` 旧断言（必发 → 不发）
- [x] T6 新增 `TestIssue679SkipEmptyPosting` 回归用例（含 PR 路径与 handler 链路）
- [x] T7 运行相关测试：`test_issue_451`（17 passed）；关联套件回归通过，基线历史失败项另行记录（见 plan.md）
- [x] T8 自审查（5 轴 + pre-PR 增量检查：黑名单/敏感扫描/commit message 全英文）
- [x] T9 提交、推送 feat/issue-679、建 PR（base=weekly-08-mys）、Issue 评论告知、A2A 通知 QA
