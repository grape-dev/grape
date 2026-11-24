# Plan: Issue #3800 — 失败分类 → 退避/熔断状态机

> 目标文件：`src/domains/runtime/engine/scheduler.py`（主）、mgmt 域 DDL/写原语、
> runtime 域 DDL/迁移、`tests/runtime/` 回归。

## 1. 单一分类出口

新增模块级常量与纯函数（不碰 DB，便于单测）：

| 符号 | 语义 |
|------|------|
| `ERROR_ENGINE_OFFLINE = "ENGINE_OFFLINE"` | 绑定引擎全部离线（环境性失败，重试无意义） |
| `ERROR_BINDING_STALE = "BINDING_STALE"` | 绑定 uid/id 在 `agent_runtimes` 中查无此行（需重绑） |
| `_ENV_FAILURE_CODES` | 上面两者元组，收口时按「不消耗 max_retry」处理 |
| `_BACKOFF_CAP_SECONDS = 1800` | 退避封顶 30min |
| `_CIRCUIT_THRESHOLD = 3` | 连续同因熔断阈值 |
| `_CIRCUIT_MIN_OFFLINE_SECONDS = 300` | 熔断离线持续门槛（防健康抖动误暂停） |
| `_OFFLINE_LOG_FINGERPRINT = "all offline (Issue #259)"` | 既有 Loki 查询指纹，新行内保留 |

`_probe_task_binding(task)`：**唯一**绑定探测 seam。返回
`{"mode": "uid"|"ids"|"", "uids": [...], "ids": [...], "members": [...], "healthy": [...],
"engine_ids": [...]}`，复用 `engine_select._engines_by_uids/_engines_by_ids/_health_ok/_norm_repo`。
- `members` 为空 → 绑定悬空（`BINDING_STALE`）；
- `members` 非空但 `healthy` 为空 → 全部离线（`ENGINE_OFFLINE`）。
两条派发路径（`_execute_autopilot_remote` 的 uid 池 / legacy id 池）共用该探测，消除同构分叉。

`_compute_backoff_next_run(cron_expr, consecutive_failures, now=None)`：
`delay = min(max(cron_period, 60) * 2**(n-1), 1800)`，返回 `now + delay` 的 ISO 串；
`cron_period` 由 croniter 连续两次 `get_next` 求差，失败回落 60s。

时间统一走模块级 `_scheduler_now()`（返回 aware local datetime），假时钟测试只 patch 这一个 seam。

## 2. 状态持久化（跨域写）

`autopilot_tasks` 归 mgmt 域，新增 5 列：

| 列 | 用途 |
|----|------|
| `consecutive_failures INTEGER NOT NULL DEFAULT 0` | 连续同因失败计数 |
| `last_error_code TEXT DEFAULT ''` | 最近一次失败的错误码（熔断判据的「同因」依据） |
| `offline_since TEXT DEFAULT ''` | 本次离线/悬空起始时间（离线时长门槛 + 重启后仍生效） |
| `backoff_until TEXT DEFAULT ''` | 退避截止时间（调度闸门） |
| `circuit_paused INTEGER NOT NULL DEFAULT 0` | 熔断自动暂停（与 `enabled` **分离**：`enabled=0` 是用户意图，`circuit_paused=1` 是系统态，可自动解除） |

`autopilot_executions`（runtime 域）新增 `error_code TEXT DEFAULT ''`。

- 新库：`mgmt/schema_ddl.py::_AUTOPILOT_TASKS_DDL` 与 `runtime/schema_ddl.py::_AUTOPILOT_EXECUTIONS_DDL` 直接带列。
- 存量库：登记式迁移 `mgmt/migrations/autopilot_failure_state.py`（202611230003）与
  `runtime/migrations/autopilot_execution_error_code.py`（202611230004），
  幂等 `_ensure_column` + `post_check`（列缺失即不盖章，下次启动重试）。
- 写原语（mgmt 本域）：`set_autopilot_failure_state` / `set_autopilot_failure_state_any`、
  `clear_autopilot_failure_state` / `clear_autopilot_failure_state_any`；
  SQL 登记在 mgmt `_ANY_QUERIES`（跨域守卫：runtime 侧不直写 mgmt 表），
  runtime 侧走既有 `cross_write.submit` 意图通道（`mgmt_lib` 优先，`*_any` 兜底）。
- 读侧：`autopilot_tasks_enabled_full` / `autopilot_task_full_row` 的列序追加 5 列，
  `_get_enabled_tasks` / `trigger_task_now` 按下标解包。

## 3. 收口裁决（`_execute_task`）

1. 每次 attempt 建 exec 后读 `(status, error, error_code)`（老库缺列时回落两列查询）。
2. `error_code ∈ _ENV_FAILURE_CODES` → **不重试、不消耗 max_retry**（D4），跳出 attempt 循环。
3. 环境性失败交给 `_handle_environmental_failure(task, exec_id, code, error)`：
   - 同因累加 `n = prev + 1`（错误码变化则重置为 1）；维护 `offline_since`；
   - `paused` 判定：`n >= 3` 且（`BINDING_STALE` 或 离线时长 ≥ 300s）；
   - 告警一次性：`BINDING_STALE` 在 `n == 1` 告警（重绑指引）；`ENGINE_OFFLINE`
     在「本次跨过熔断门槛」时告警；已处于 `circuit_paused` 不再重复告警；
   - `next_run_at = _compute_backoff_next_run(cron, n)`，与失败状态同一次跨域写落库。
4. 成功 / skipped / cancelled / 普通失败 → `_clear_failure_state`（若曾计数或暂停）并推进正常 `_compute_next_run`。
5. 终态 ERROR 单行结构化（含指纹子串），派发侧的 `all offline` WARNING 删除/并入该行。

## 4. 调度闸门（`_scan_task_once`）

把 `scheduler_loop` 的单任务单轮裁决抽成同步函数（供循环与假时钟测试共用）：

```
circuit_paused?  → 尝试解除（绑定恢复健康/uid 重新存在）→ 解除后本轮继续，否则 'paused'
cron 未到期      → 'not-due'
backoff_until 未过 → 'backoff'（不建 exec，只打一条 info）
否则             → 'dispatch'
```

手动触发 `trigger_task_now` 不受 `backoff_until` 拦截（既有独立入口）。

## 5. 告警

`_alert_task_failure(...)`：结构化 `log.error`（`error_code` / `engine_ids` /
`consecutive_failures` / 重绑指引）+ `_notify_subscribers(task_id, exec_id, "failed", summary, {})`。
幂等由两处保证：同一 episode 只在跨越门槛时调用一次；`_notify_subscribers` 自身
`notified=0` 抢占保证同一 exec 只发一次卡片。

## 6. 测试 seam 与回归

新增 `tests/runtime/test_issue_3800_autopilot_offline_backoff.py`（默认运行的普通用例，无 e2e）：
- 假时钟驱动 `_scan_task_once` + `_execute_task`（`_execute_autopilot_agent` 打桩写
  `failed + error_code`），60s cron，跑满 3600s：断言 exec ≤ 4、退避序列、
  `_notify_subscribers` 恰 1 次、终态 `circuit_paused=1`；
- `BINDING_STALE`：绑定 uid 不存在 → 首派即告警、进入退避；
- 恢复：引擎转健康 → 下一轮解除暂停、续跑成功、`consecutive_failures` 归零；
- 纯函数：`_compute_backoff_next_run` 的 1/2/4/8/16 与封顶 1800；`_probe_task_binding` 三态；
- 迁移：两条登记项存在且 `post_check` 可判定、幂等补列。

## 关键决策（承接 Spec D1–D5）

- D1 状态存 `autopilot_tasks`（重启不丢、免全表回溯）；
- D2 阈值 3 次 + 自动解除；D5 加「离线 ≥ 5min」门槛防抖；
- D3 不引指标中心，用结构化日志字段供 Loki 聚合；
- D4 环境性失败不消耗 `max_retry`；
- 新增「暂停位与 enabled 分离」（`circuit_paused`），避免自动解除时抹掉用户停用意图。