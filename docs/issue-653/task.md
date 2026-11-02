# Task Breakdown: Issue #653

- [x] T1 pipeline_config.py：get_scene_engine_binding 四态 API + get_scene_engine_ids 薄包装
- [x] T2 engine_select.py：strict_scene_engines 开关（默认 true）+ get_scene_pool_engine_ids bound-空候选收口 + scene_dispatch_failure_reason 增补
- [x] T3 service.py：enqueue_task 空 pool 候选前置校验
- [x] T4 event_pipeline/webhook：a2a 场景解析异常告警补强
- [x] T5 help/usage/runtimes.md：engine_runtime_ids 语义与开关文档
- [x] T6 tests/test_issue_653_engine_binding.py 全分支单测 + 存量回归
- [x] T7 自审查（5 轴 + pre-pr 增量检查）→ 提交 → PR（base=weekly-08-mys）
