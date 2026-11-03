# Tasks: Issue #630

- [ ] T1 `verify_skill_materializable` 实现（service.py）+ 单测
- [ ] T2 `_enqueue_pipeline_task` enqueue 前门禁（event_pipeline.py）
- [ ] T3 `/api/skills/by-id` name→sid 解析（routes/skills.py）+ 集成测试
- [ ] T4 `create_skill` 空壳行告警（user/db.py，发布链路断点修复）
- [ ] T5 回归：相关既有测试全绿（test_issue_287 / test_api_routes / test_user_db 等）
- [ ] T6 自审查（5 轴 + pre-pr 增量检查）→ 建 PR base=weekly-08-mys → 评论 + A2A QA
