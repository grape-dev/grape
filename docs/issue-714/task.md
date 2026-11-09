# Task: Issue #714 实现拆解

- [x] T1 需求文档 docs/issue-714/spec.md（镜像已发布 Spec）
- [x] T2 方案设计 docs/issue-714/plan.md
- [x] T3 测试先行：test_git_repo_api.py 新增 5 用例（+ / 中文+空格 / ASCII 回归 / 404 透传 / 单元级）
- [x] T4 实现 `_vcs_url()` subpath 逐段 quote（GREEN）
- [x] T5 全量回归 pytest tests/test_git_repo_api.py（53 passed；1 失败为基线既有，stash 验证无关）
- [x] T6 自审查 + pre-pr 检查 → commit → push → PR（base=weekly-08-mys）→ Issue 评论 → A2A QA
