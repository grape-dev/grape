# Task — Issue #595

- [ ] T1 编码：`_inject_issue_pr_env` PR 号提取链增加嵌套兜底三键（pull_request.number / merge_request.number / object_attributes.iid），沿用 lstrip("#").isdigit() 校验
- [ ] T2 文档：`help/usage/agent-environment-variables.md` PR_NUMBER 行更新提取来源
- [ ] T3 测试：新增 `tests/test_issue_595_pr_number_nested_payload.py`（关联 Issue / PR 订阅两形态 + 优先级 + 非数字防御）
- [ ] T4 回归：运行 528 注入测试与 event_pipeline 相关既有测试
- [ ] T5 自审查（5 轴 + pre-pr 检查）→ commit → push → 建 PR（base=weekly-09）→ Issue 评论 → A2A QA → tasks complete
