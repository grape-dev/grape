# Task: Issue #632 里程碑路由实现拆解

- [x] T1 核对 #577 进展（pending_review、无 PR、无实现 → 无重复）
- [x] T2 需求文档 docs/issue-632/spec.md
- [x] T3 方案设计 docs/issue-632/plan.md
- [ ] T4 测试先行：test_git_repo_api.py 新增 milestones create/update RED 断言
- [ ] T5 实现 POST/PATCH milestones 路由 + Body 模型（GREEN）
- [ ] T6 test_openapi.py 契约映射 + docs/openapi/10-git-repos.md §6 文档
- [ ] T7 全量回归（pytest tests/test_git_repo_api.py tests/test_openapi.py）
- [ ] T8 自审查 + pre-pr 检查 → commit → push → PR（base=weekly-08-mys）→ Issue 评论 → A2A QA
