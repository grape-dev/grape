# Task: Issue #1408 — 已完成需求可手动撤销回待审核

- [x] T1 状态机新增 `completed → pending_review` 出口（`src/api/issues_center.py`）。
- [x] T2 状态更新入口实现撤销：带 `AND status='completed'` 守卫的单条 UPDATE（回退 status/stage、清空完成字段、刷新 `updated_at`），审计记录实际 from_stage 与 prev_completed_at/prev_completed_reason（`src/domains/runtime/routes/issues.py`、`src/domains/mgmt/auth/audit.py`）。
- [x] T3 更新 `tests/runtime/test_issue_627_production_release.py` 终态断言与 docstring。
- [x] T4 新增 `tests/runtime/test_issue_1408_revert_completed.py`：成功迁移/回读、stage 回退、字段清空、updated_at 刷新、审计（实际 stage + 撤销前完成信息）、非属主 403、会话 token 403、非法迁移 400、并发态变更 409、幂等、回归。
- [x] T5 运行相关测试并全绿（`pytest tests/ -v`、`pytest tests/ --cov`，覆盖率门槛 70%）。
- [x] T6 提交、推送、创建 PR、Issue 评论、A2A QA、tasks complete。

## 复审补充任务（PR #1009 审查意见）

- [x] T7 页面入口：`completed` 状态渲染「撤销完成」按钮（本人或仓库管理员可见，非授权不渲染），hover 过渡 + 双语 tooltip 功能说明 + `_grapeConfirm` 二次确认 + 成功 toast 与列表/流水线刷新；删除 `@deprecated changeTaskStatus` 死代码。
- [x] T8 后端鉴权：`PUT /issues/{id}/status` 的 `completed → pending_review` 增加仓库 Owner/Admin 放行（`check_repo_access` + `get_repo_admin_identity` fail-closed），审计记录 `actor_role`/`role_source`。
- [x] T9 缓存与双语：`issues.js?v=138`、`app.css?v=109` 及 ratchet 冻结映射同步；`tasks.json` zh/en 文案 key 齐备。
- [x] T10 新增前端回归 `tests/api/js/test_issue_1408_revert_completed_entry.js` 与后端 `TestAdminRevert`；相关测试全绿。