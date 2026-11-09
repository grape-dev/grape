# Task: Issue #491

- [x] T1 RED：新增 tests/test_issue_491_repo_roles.py（4 单元用例）+ tests/test_issue_393_admin_confirm.py 新增类（3 集成用例：跨用户 admin 成功 / 非 admin 403 / owner 成功）
- [x] T2 GREEN：src/auth/repo_roles.py `_check_gitcode_admin` 加 role_name=="Admin" 分支
- [x] T3 回归：#393 全量 15 用例 + 相关套件通过
- [x] T4 文档 docs/issue-491/{spec,plan,task}.md
- [ ] T5 pre-pr 检查 + 自审查
- [ ] T6 PR + Issue 评论 + A2A QA + tasks complete
