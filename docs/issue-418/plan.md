# Plan: Issue #418 — 实现方案

## 改动清单
| 文件 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | `_fail_event(seq, reason, exit_code=1)`：补形参；docstring 记录幂等语义（events 终态保护 + agent_tasks 非终态限定） |
| `src/api/routes/webhook.py` | ① pick_engine=None 早退 → `_fail_event(seq, "A2A 派发失败：无可用引擎（pick_engine=None…）")`；② auto-select 空集 → `"…auto-select 空集…"`；③ auto-select 异常 → `"…auto-select 异常: {e}"`；④ try 兜底 → `"A2A 派发启动异常: {e}"`；⑤ `_fail_scene_all_offline` 异常兜底同样改 `_fail_event` |
| `src/runtime/service.py` | `_fail_task_pool_unreachable` / `_fail_task_pool_unhealthy` UPDATE 补 `exit_code=COALESCE(exit_code, 1)`（#369 字段完整性收口） |
| `tests/test_issue_418_a2a_fail_event.py` | 新增：4 早退（TestClient + mock pick_engine / db 注入异常）+ 幂等 + #369 判死 |

## 测试策略
- legacy 模式（`GRAPE_PIPELINE_MODE=legacy`）直达 webhook 4 处早退分支；
  spot3 用 `_BoomDb` 包装器仅对 auto-select 特征 SQL 抛错，其余透传。
- 断言 events 行 status=failed / exit_code=1 / completed_at 非空 / result 含分支化原因。
- 幂等：同 seq 两次 `_fail_event`，第二次不覆盖第一次 result/completed_at。

## 风险与缓解
- `_fail_event` 出参变化：默认值兼容，既有 30+ 调用点零改动（回归 test_issue_368 通过）。
- 早退分支返回值（status/seq）保持原语义，不改变调用方降级行为。
