# Plan: Issue #393 — 仓库 admin 成员确认需求完成

> 依据 docs/issue-393/spec.md | TDD：先写测试（RED）→ 实现（GREEN）→ 自审查

## 任务拆解（原子提交）

| # | 任务 | 文件 | 提交 |
|---|------|------|------|
| T1 | 角色校验 helper `src/auth/repo_roles.py`（GitCode access_level≥50/permission=admin/owner；GitHub role_name=admin；fail-closed） | src/auth/repo_roles.py | feat(#393): repo admin role check helper |
| T2 | 审计事件类型 `issue_admin_confirmed` | src/auth/audit.py | 同 T1（原子：helper+事件类型） |
| T3 | 端点 `POST /api/issues/{id}/admin-confirm-done`：403/404/400/幂等/状态+流水线同步/审计/VCS 关闭 | src/api/routes/issues.py | feat(#393): admin confirm-done endpoint |
| T4 | 前端 admin 入口 + 置灰 + i18n（zh/en） | issues.js, locales | feat(#393): admin confirm entry UI |
| T5 | 测试 tests/test_issue_393_admin_confirm.py（正例/反例 403/幂等/审计/流水线同步） | tests/ | test(#393): admin confirm coverage |

## 执行顺序

1. T1+T2（helper+事件类型）→ 单测可 import
2. T5 先行编写（RED）——端点未实现时 404
3. T3（端点 GREEN）
4. T4（前端）
5. 回归：`pytest tests/test_issue_393_admin_confirm.py tests/test_tasks_lifecycle.py tests/test_issue_209_close_vcs.py tests/test_issues_api.py tests/test_repo_cascade_cleanup.py`
6. 自审查 5 轴 → PR

## 测试矩阵

| 用例 | 期望 |
|------|------|
| admin（permission api 返回 access_level=50）确认 processing 需求 | 200，status=completed，stage=done，审计+1 |
| admin 确认 completed 需求 | 200 already_done，无二次审计 |
| 非 admin（access_level=30） | 403 |
| owner 本人（username==owner） | 200 |
| 未绑定 gitcode 的用户 | 403 |
| VCS API 异常 | 403（fail-closed） |
| cancelled 需求 | 400 |
| tk_ 会话 token | 403（require_no_session_token） |
| agent_tasks 有 running 任务 | 确认后置 cancelled |
| events 有 running 事件 | 确认后置 completed |
| 审计 detail | repo_key/issue_id/issue_number/operator_id 可查 |
