# Task: Issue #564

- [x] T1 新增 src/runtime/skill_materialize.py（原子物化/校验/回退/降级状态）
- [x] T2 event_pipeline _materialize_direct_workdir 接入 + evt 降级标注 + prompt 降级说明
- [x] T3 event_pipeline _execute_agent_inner 事件 agent_logs 降级标注
- [x] T4 worker agent.py zip/skill_files 物化改原子 + 回退 + 不静默空注入
- [x] T5 prompt_builder / scheduler autopilot prompt 技能内容位置说明
- [x] T6 tests/test_skill_materialize.py 覆盖成功/回退/降级/原子性
- [x] T7 全量相关单测通过 + 自审查 + PR
