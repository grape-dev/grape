# Plan — Issue #593

## 任务拆解
1. TDD RED：更新 `tests/test_repo_migrate_orgs.py`
   - 将 `test_migrate_orgs_personal_org_included` 改为 `test_migrate_orgs_personal_org_excluded_from_orgs`：断言 `orgs` 中不含 `is_personal=true` 条目。
   - 新增 `test_migrate_orgs_personal_field_still_returned`：断言 `personal` 字段仍等于 username。
   - 新增 `test_migrate_orgs_team_orgs_still_listed`：同时创建个人组织 + 团队组织，断言 orgs 含团队、不含个人。
2. 运行测试确认 RED（个人过滤测试失败、其余通过）。
3. GREEN：修改 `src/api/routes/repos.py` `list_migrate_orgs`，组包循环增加 `if r[3]: continue`。
4. 运行测试确认 GREEN（全部通过）。
5. 自审查（5 轴 + 增量检查）。
6. 提交、推送、建 PR、评论、A2A 通知 QA、tasks complete。

## 风险
- 旧测试 `test_migrate_orgs_personal_org_included` 与新需求直接冲突，必须同步更新（否则 RED 永远存在）。
- DB 状态污染：测试间共享 test_sessions.db，`_ensure_user` 已用固定 username 复用记录，新测试沿用同模式。
