# Issue #3914 任务拆解（task）

| # | 切片 | 落点 | 状态 |
|---|------|------|------|
| T1 | B 红用例：`_rebuild_evt_from_task` 桥接 `skill_id`/`_skill_path`/`__obs_key__`；`enqueue_task` 回写列 | tests/runtime/test_issue3914_direct_skill_bridge.py | [ ] |
| T2 | B 实现：service `_resolved_skill_id` 回写 + local_claimer 桥接 | service.py / local_claimer.py | [ ] |
| T3 | C 实现与验证：`_skill_primary` 复用 `_skill_obs_key` → `get_skill_zip_bytes` | event_pipeline.py | [ ] |
| T4 | A：PR 订阅补派/重派传 `resolve_scene_node_skill_id` | event_pipeline.py | [ ] |
| T5 | D：回退结果补 `skill_name` + direct verify 传 `skill_fallback` | skill_materialize.py / event_pipeline.py | [ ] |
| T6 | E：`fill_scene_nodes_skill_id` + 保存挂载点 | pipeline_config.py / scenes.py | [ ] |
| T7 | E：存量回填脚本（dry-run 对照清单 + `--apply`） | scripts/backfill_scene_nodes_skill_id.py | [ ] |
| T8 | 回归：既有 skill/claimer/scene 相关用例全绿 | tests/runtime, tests/api | [ ] |
| T9 | 自审查（5 轴 + 开源前置增量检查）→ PR → Issue 评论 → A2A QA | — | [ ] |

## 依赖
- T4/T5 依赖 T2 的桥接语义（同批上线，D 不得单独上线）
- T6/T7 与 T1-T5 无耦合，可并行

## 反向回归（必须保持）
- 任务行 `skill_id=0` 且无 `__obs_key__` → 现有「按名 + 官方目录」降级不变
- 绑定技能确缺失（materialize `degraded`）→ 不传 `skill_fallback`，仍 `[skill_mismatch]` fail-fast