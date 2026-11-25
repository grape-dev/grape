# Spec：Issue #4375 auto-advance 回滚读 events 落到 mgmt 库恒 1146

> 来源：Issue #4375 正文 + Issue Agent 发布的 Spec（评论 203746546）。本文件为实现侧对齐版，
> 补齐落地后的代码位置与验证口径。

## Objective

恢复「auto-advance 下游事件失败 → `issue.stage` 由 `guard.to` 退回 `guard.from`」守卫，使其在
mgmt / runtime / insight 分库拓扑下真正执行。

该守卫在 mgmt 进程 100% 失败（异常被 `except` 吞成 WARNING），下游失败后 stage 停在从未运行的
节点：看板显示「已进入但没跑过」的阶段，断链只能人工 re-dispatch。

## 现状（改动前）

- `src/domains/runtime/pipeline/event_pipeline.py::_rollback_auto_advance_stage`（本分支 4335 行）
  用 `src.kernel.session_manager.db` 直查 `events`。`events` 归 runtime 域
  （`grape_prod_runtime.events`），mgmt 进程该通道解析到 mgmt 库 → 恒
  `(1146, "Table 'grape_prod_mgmt.events' doesn't exist")`。
- 异常分支 `log.warning(...)` 后 `return`，回滚动作永不执行（生产 `grape-mgmt` 约 5 条/小时）。
- 同源残留口子：`src/domains/runtime/public/__init__.py::_any_cursor` 自带第二套判定
  「`uses_own_dsn("runtime") ? 域连接 : 进程主库`」，三库拓扑缺 `GRAPE_DSN_RUNTIME` 时静默回落
  进程主库。`skill` 表读取（`src/api/skill_display.py` 的展示名解析链）经它打到 mgmt 库，
  约 8 条/小时 `Table 'grape_prod_mgmt.skill' doesn't exist` 并降级 display_name。

## Scope

### In

1. `src/domains/runtime/public/__init__.py`
   - 新增具名查询 `event_routing_payload`：`SELECT repo_key, issue_id, payload FROM events WHERE seq=? LIMIT 1`，
     以 `event_routing_payload(seq)` 公开（返回 `{repo_key, issue_id, payload}`；无行/非法 payload → `None`）。
   - `_any_cursor` 收口到 `db_registry.cursor_for("runtime")`：`domain` → 域连接，`local`
     （单库 / 本域进程）→ 进程主库，三库缺 DSN → 显式 `DomainUnavailable`，**绝不回落**进程主库。
2. `src/domains/runtime/pipeline/event_pipeline.py`
   - `_rollback_auto_advance_stage` 删掉 `session_manager.db/db_lock` 直查，改调 runtime 域 public 只读口；
     短路语义（seq 非法 / 无行 / payload 非 dict / 无 `stage_guard` / `from==to` / 当前 stage ≠ `guard.to`）不变。
   - 失败日志 WARNING → ERROR，携带 `seq`、`stage_guard` 影响的 issue 号、
     `db_registry.conn_facts("runtime")`（`route / target_db / pid / host`，脱敏）。
3. `src/api/skill_display.py`：已走 `runtime_public`，随游标原语修复生效，回归确认不再降级。

### Out

- 不回填历史已卡住的 `issue.stage`（另立巡检回填单）。
- 不改 `events` / `skill` 表域归属与 DDL，不做数据迁移。
- 不新增 Loki 告警规则，不改展示名回退优先级语义。

## Acceptance Criteria（对应测试）

| AC | 覆盖用例 |
|----|---------|
| 双库拓扑下回滚不出现 1146，stage 由 `guard.to` 回退 `guard.from` | `test_dual_db_rollback_reads_runtime_domain_and_restores_stage`、`test_dual_db_rollback_does_not_touch_process_db` |
| 下游终态 failed 用例断言 `issue.stage` 已回退 | `test_failed_downstream_event_rolls_back_issue_stage` |
| 四类短路（无 stage_guard / stage 已变 / payload 非法 / seq 非法）不回退不抛异常 | `test_bad_payload_short_circuits`、`test_stage_already_moved_short_circuits`、`test_invalid_seq_short_circuits`、`test_missing_event_row_short_circuits`、`test_equal_from_to_short_circuits` |
| `resolve_skill_display_name` 双库返回 display_name 且不降级 | `test_resolve_skill_display_name_reads_runtime_domain_in_dual_db` |
| 缺 DSN 显式 `DomainUnavailable` / ERROR 带 conn_facts，不回落 mgmt | `test_missing_runtime_dsn_raises_domain_unavailable`、`test_rollback_failure_logs_error_with_conn_facts` |
| 现有测试全通过 | `pytest tests/api tests/runtime`（其余全绿；`tests/api/test_a2a_response_contract.py` 两例为基线既有红） |

## 成功判据（上线后）

- 1 小时内 `Pipeline: auto-advance stage rollback failed` 与
  `resolve_skill_display_name failed ... doesn't exist` 计数归零；
- 若仍出现，ERROR 行的 `route=domain` 说明打到 runtime 库（问题在数据），`route=unavailable`
  说明该主机缺 `GRAPE_DSN_RUNTIME`（配置问题），可与「旧进程未重启」区分。