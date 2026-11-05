# Issue #887 任务拆解（task）

- [x] T1 判定规则：`skill_materialize.verify_skill_execution_consistency` + `_scan_materialized_skills`
- [x] T2 健康检查项：`health.check_skill_consistency`（mismatch/declared/unbound 口径）
- [x] T3 来源标注：`task_log_stream.build_skill_source_message` + `_to_payload.skill_source` 透传
- [x] T4 worker 执行前硬校验：`agent._skill_consistency_gate`（拒绝 → `[skill_mismatch]` 任务 failed + 来源日志上报）
- [x] T5 派发携带绑定 skill：`service.enqueue_task` env `GRAPE_BOUND_SKILL`
- [x] T6 direct 端硬校验：`event_pipeline._materialize_direct_workdir` → `evt._skill_consistency`；`_execute_agent` fail-fast + agent_logs 标注
- [x] T7 autopilot direct 硬校验：`scheduler._execute_autopilot_direct` 引擎启动前 fail-fast
- [x] T8 测试：`tests/test_issue887_skill_consistency.py`（含 seq=46108 回归用例）+ 既有套件基线对比零回归
- [x] T9 文档：`docs/issue-887/{spec,plan,task}.md`
