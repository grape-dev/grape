# Task — Issue #1579

- [x] T1 编码：`_inject_issue_pr_env` 增加 `note_hook_pr` 独立解析（noteable_iid 优先、标题兜底、关联校验、失败标记）
- [x] T2 文档：`help/usage/agent-environment-variables.md` PR_NUMBER 提取来源补充 note_hook_pr 分支
- [x] T3 测试：新增 `tests/test_issue_1579_note_hook_pr_number.py`（payload / 标题 / 跨 Issue / 失败 / 不回归）
- [ ] T4 回归：运行 #595 / #528 注入测试及 event_pipeline 相关既有测试
- [ ] T5 自审查（5 轴 + pre-pr 检查）→ commit → push → 建 PR（base=weekly-mys）→ Issue 评论 → A2A QA → tasks complete