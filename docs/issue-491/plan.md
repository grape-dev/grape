# Plan: Issue #491 — 管理员确认完成跨用户回归修复

> 依据 docs/issue-491/spec.md | TDD：先红（复现测试）→ 绿（最小修复）→ 回归

## 根因
`src/auth/repo_roles.py::_check_gitcode_admin` 判定链缺 `role_name` 分支；GitCode 自定义管理员角色（role_name="Admin"、access_level=20、permission="custom"）被判非 admin → 403，与前端 `/user/repos` 列表口径（permission.admin=true → Admin）不一致。

## 任务拆解（原子提交）

| # | 任务 | 文件 | 提交 |
|---|------|------|------|
| T1 | RED：新增单元+集成测试（role_name=Admin 自定义角色 → True/200；Developer/Maintainer → False/403） | tests/test_issue_491_repo_roles.py、tests/test_issue_393_admin_confirm.py（新增类 TestIssue491CrossUserAdminConfirm） | test(#491): repo admin role check misses role_name (custom admin roles) |
| T2 | GREEN：`_check_gitcode_admin` 判定链最前加 `role_name=="Admin"`（不区分大小写）分支 | src/auth/repo_roles.py | fix(#491): accept role_name=Admin in gitcode admin check |
| T3 | 文档 | docs/issue-491/{spec,plan,task}.md | docs(#491): root cause & fix docs |

## 测试 seam（公开接口）
- 单元：`src.auth.repo_roles._check_gitcode_admin(token, owner, repo, username)`（模块公开行为：GitCode admin 判定口径）+ `get_repo_admin_identity`（GitHub/owner 路径不回归）。
- 集成：`POST /api/issues/{id}/admin-confirm-done`（HTTP 端点，mock `_req` 返回真实形态 payload）。

## 执行顺序
1. T1 红测试（跑一遍确认失败）
2. T2 最小实现（跑测试转绿）
3. 回归：pytest tests/test_issue_393_admin_confirm.py tests/test_issue_491_repo_roles.py + 相关套件
4. pre-pr 检查（黑名单拦截 + 增量敏感扫描）→ 自审查 → PR → Issue 评论 → A2A QA → tasks complete

## 风险
- role_name 判定放宽：仅认 GitCode 官方返回值 "Admin"（大小写不敏感），Developer/Maintainer/自定义非管理角色不受影响；GitHub 路径走 role_name==admin 已有等价逻辑，无需改。
