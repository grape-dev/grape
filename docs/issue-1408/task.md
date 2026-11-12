# Task: Issue #1408 — 已完成需求可手动撤销回待审核

- [x] T1 状态机新增 `completed → pending_review` 出口（`src/api/issues_center.py`）。
- [x] T2 状态更新入口实现撤销：带 `AND status='completed'` 守卫的单条 UPDATE（回退 status/stage、清空完成字段、刷新 `updated_at`），审计记录实际 from_stage 与 prev_completed_at/prev_completed_reason（`src/domains/runtime/routes/issues.py`、`src/domains/mgmt/auth/audit.py`）。
- [x] T3 更新 `tests/runtime/test_issue_627_production_release.py` 终态断言与 docstring。
- [x] T4 新增 `tests/runtime/test_issue_1408_revert_completed.py`：成功迁移/回读、stage 回退、字段清空、updated_at 刷新、审计（实际 stage + 撤销前完成信息）、非属主 403、会话 token 403、非法迁移 400、并发态变更 409、幂等、回归。
- [x] T5 运行相关测试并全绿（`pytest tests/ -v`、`pytest tests/ --cov`，覆盖率门槛 70%）。
- [x] T6 提交、推送、创建 PR、Issue 评论、A2A QA、tasks complete。