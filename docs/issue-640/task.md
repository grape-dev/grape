# Tasks: Issue #640 拆解与执行记录

- [x] T1 provider 映射：gitcode `_RULES[("issues","update")]` milestone_id → milestone（src/vcs/providers/gitcode.py）
- [x] T2 必填补齐：update_issue gitcode 分支自动补 repo + 回取 title（src/api/git_repo_api.py）
- [x] T3 回读校验：PATCH 后 GET 回读；未落库 → 502 明确错误；成功回显 milestone
- [x] T4 测试：tests/test_issue_640_milestone_contract.py（映射/补齐/回显/错误路径/GitHub 不变）
- [x] T5 文档：help/api/10-git-repos.md 契约说明 + docs/issue-640/{spec,plan,task}.md
- [x] T6 验证：新增测试 6/6 通过；存量相关测试与基线一致（无回归）
