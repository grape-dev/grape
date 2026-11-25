# Task：Issue #4375 任务拆解与完成状态

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1 | 新增 `event_routing_payload` 具名查询 + 公开只读口 | `src/domains/runtime/public/__init__.py` | ✅ |
| 2 | `_any_cursor` 收口到 `db_registry.cursor_for("runtime")`（缺 DSN 显式 `DomainUnavailable`） | `src/domains/runtime/public/__init__.py` | ✅ |
| 3 | 回滚函数改走 public 只读口，删 `session_manager.db/db_lock` 直查 | `src/domains/runtime/pipeline/event_pipeline.py` | ✅ |
| 4 | 回滚失败日志 WARNING → ERROR（seq + issue + stage_guard + `conn_facts("runtime")`） | `src/domains/runtime/pipeline/event_pipeline.py` | ✅ |
| 5 | 双库拓扑用例（读走 runtime 域 / stage 回退 / 不触达 mgmt 哨兵） | `tests/runtime/test_issue_4375_auto_advance_rollback.py` | ✅ |
| 6 | 下游终态 failed 端到端用例（真实 sqlite 行，断言 `issue.stage` 回退） | 同上 | ✅ |
| 7 | 四类短路用例（无 stage_guard / stage 已变 / payload 非法 / seq 非法 + 无行 + from==to） | 同上 | ✅ |
| 8 | `resolve_skill_display_name` 双库回归（返回 display_name、无 WARNING 降级） | 同上 | ✅ |
| 9 | 缺 DSN 用例（显式 `DomainUnavailable` + ERROR 事实串 + 不回落 mgmt） | 同上 | ✅ |
| 10 | 源码棘轮（回滚不再直查 / `_any_cursor` 只认 `cursor_for` / events 路由读全仓唯一） | 同上 | ✅ |
| 11 | 全量回归 + `make lint` + `scripts/check-arch.py` | — | ✅ |

## 门禁结果

- `pytest tests/runtime/test_issue_4375_auto_advance_rollback.py`：20 passed。
- `pytest tests/runtime/test_repogit.py tests/runtime/test_issue_3482_workspace_order.py tests/runtime/test_issue_2245_git_writable.py`：66 passed。
- `pytest tests/api tests/runtime`：除基线既有红（`tests/api/test_a2a_response_contract.py` 两例，
  在 `39a7c5938` 上同样失败）外全绿。
- `make lint`：rc=0。`python3 scripts/check-arch.py`：PASS（R5 无新增模块级环）。

## 未做（Out of Scope）

- 历史已卡 `issue.stage` 回填；`events`/`skill` DDL 与域归属调整；Loki 告警规则；
  展示名回退优先级语义变更。