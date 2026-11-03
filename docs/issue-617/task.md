# Issue #617 Task Breakdown

- [x] T1 db helper `resolve_official_checker_skill(agent_type)`（src/user/db.py）
- [x] T2 `materialize_skill_with_fallback` agent_type 回退分支（src/runtime/skill_materialize.py）
- [x] T3 direct 端接线 + `--skill_id` 提示（src/api/event_pipeline.py）
- [x] T4 claim 端 `_attach_skill_obs` skill_fallback 下发（src/api/routes/runtime.py）
- [x] T5 worker 端消费 skill_fallback（src/runtime/agent.py）
- [x] T6 单测（tests/test_skill_materialize.py 新增用例）
- [x] T7 自审查（5 轴 + pre-pr 增量检查）→ PR → A2A QA
