# Task: Issue #393

- [x] T1 `src/auth/repo_roles.py` — repo admin 角色校验 helper（GitCode/GitHub，fail-closed）
- [x] T2 `src/auth/audit.py` — 新增 `issue_admin_confirmed` 事件类型
- [x] T3 `src/api/routes/issues.py` — `POST /issues/{id}/admin-confirm-done`（403/幂等/同步/审计/VCS）
- [x] T4 `issues.js` + locales — admin 专用入口、非 admin 置灰
- [x] T5 `tests/test_issue_393_admin_confirm.py` — 11 用例全绿
- [x] 回归：lifecycle / 209_close_vcs / issues_api / openapi 同步检查通过
