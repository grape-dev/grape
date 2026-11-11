# Task — Issue #1579

- [x] T1 编码：`_inject_issue_pr_env` 增加 `note_hook_pr` 解析（noteable_iid 优先、结构化 URL 兜底、失败标记）
- [x] T2 文档：`help/usage/agent-environment-variables.md` PR_NUMBER 提取来源补充 note_hook_pr 分支
- [x] T3 测试：新增 `tests/test_issue_1579_note_hook_pr_number.py`（payload / URL 兜底 / 标题不猜号 / 失败 / 不回归）
- [x] T4 回归：运行 #595 / #528 注入测试及 event_pipeline 相关既有测试
- [x] T5 自审查（5 轴 + pre-pr 检查）→ commit → push → 建 PR（base=weekly-mys）→ Issue 评论 → A2A QA → tasks complete
- [x] T6 QA 复审修复：移除生产不可达的标题猜号与关联校验，改从结构化 URL 兜底；note_hook_pr 与普通事件收敛到单一 `_extract_pr_number`；更新 spec/plan 与 PR 说明（提示 grape-skills runbook Step A0 不得声明可从标题提取）