# Task: Issue #654 开发拆解

- [x] T1 文档：spec/plan/task（docs/issue-654/）
- [x] T2 engine_select.py：新增 remap_scene_engine_bindings + remap_engine_references（含结构化事件日志）
- [x] T3 settings.py：change_credential_scope user 分支引擎行 owner_id 与 register 同源 + 替换路径 capabilities/tags_user_set 迁移 + 引用 remap 挂钩 + 事件日志
- [x] T4 runtime.py：register upsert 键收敛为 (credential_id, lower(name)) + 派生属性原地同步 + 事件日志
- [x] T5 回归测试：tests/test_issue_654_scope_engine_stability.py（往返 id/priority/标签不变、引用 remap、事件可追溯）
- [x] T6 自审查（5 轴 + pre-pr 增量检查）→ 建 PR base=weekly-08-mys → Issue 评论 → A2A QA
