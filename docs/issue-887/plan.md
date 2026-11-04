# Issue #887 方案设计（plan）

## 判定规则（单一事实源：`skill_materialize.verify_skill_execution_consistency`）

绑定 skill（任务 `skill` 字段 = 仓库级/场景级 agent 绑定解析值）执行前判定：

| 场景 | actual | source | 结果 |
|------|--------|--------|------|
| 绑定已物化（`skill_dir/<bound>/SKILL.md`） | bound | bound | 放行 |
| 绑定为空 | - | unbound | 放行（本地缓存仅此时兜底） |
| 服务端明示回退（#617 `skill_fallback`）已物化 | fallback | declared_fallback | 放行 + mismatch 标注 |
| 绑定未物化 + 本地缓存存在其它技能 | leftover | local_cache | **拒绝执行（fail-fast）** |
| 绑定未物化 + 仅 FALLBACK.md / 空缓存 | bound | degraded_recovery / missing | 放行（#607 权威取回） |

健康检查口径：`health.check_skill_consistency(bound, actual, declared)` ——
绑定非空且不一致且非声明回退 → `ok=False`（拒绝项）。

## 改动点

1. `src/runtime/skill_materialize.py`：新增 `verify_skill_execution_consistency()`
   （服务端/worker 共用，零 DB 依赖）+ `_scan_materialized_skills()`。
2. `src/runtime/health.py`：新增 `check_skill_consistency()` 健康检查项。
3. `src/runtime/task_log_stream.py`：新增 `build_skill_source_message()`；
   `_to_payload` 透传 `skill_source` 结构化字段（绑定值 vs 实际值）。
4. `src/runtime/agent.py`：新增 `_skill_consistency_gate()`（async），`_execute_task`
   物化后、引擎启动前调用——上报来源日志 + 不一致返回 `{"_error": "[skill_mismatch] ..."}`
   （`_wrap` 置任务 failed，显式可见）。
5. `src/runtime/service.py`：`enqueue_task` 注入 `env.setdefault("GRAPE_BOUND_SKILL", skill)`
   （派发 payload 携带绑定 skill，worker/日志可观测）。
6. `src/api/event_pipeline.py`：`_materialize_direct_workdir` 末尾执行校验落
   `evt["_skill_consistency"]`；`_execute_agent` 不一致 → agent_tasks 置 failed +
   `_fail_event`（diag code=skill_mismatch）+ agent_logs 技能来源标注。
7. `src/runtime/scheduler.py`：`_execute_autopilot_direct` 引擎启动前校验，
   不一致 → `_finalize_autopilot_task` failed + `_update_execution_result` failed。

注：`engine_select.py` 仅做引擎选择（无技能决策逻辑），绑定 skill 经
`service.enqueue_task`/任务行 `skill` 字段下发，不在 engine_select 改动。

## 测试
`tests/test_issue887_skill_consistency.py`（20 用例）：verify 五分支 / health 口径 /
来源标注与 SSE payload 透传 / worker gate 拒绝+日志（seq=46108 回归：绑定
caserun-prep + 缓存 casegen-prep → `[skill_mismatch]` 拒绝）/ direct 端判定 /
enqueue 绑定 env。既有套件（skill_materialize / runtime_agent / dispatch 系列）
基线对比零回归（预存失败与改动前完全一致）。
