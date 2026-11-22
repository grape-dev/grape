# Spec: Issue #3800 — Autopilot 绑定引擎永久离线：ENGINE_OFFLINE 退避 + 熔断 + 结构化告警

> 基线：`arch-weekly-11-ljt` | 类型：缺陷修复（P2，生产可观测静默失效）

## 目标

任务绑定的引擎**永久离线**时，`autopilot` 调度不再「每个 cron tick 建一条秒失败的 exec」，
改为环境性失败通道：`ENGINE_OFFLINE` 错误码 → 指数退避（封顶 30min）→ 连续 3 次同因熔断
（暂停 + 一次性结构化告警）→ 引擎恢复后自动解除续跑；并对失效的任务引擎绑定做派发前分类
（`BINDING_STALE`）。

成功标准（可量化）：

- 60s cron + 引擎持续离线 1 小时，新建 `autopilot_executions` ≤ 4 条（修复前 60 条）。
- 同因熔断告警恰好 1 次，携带 `error_code` / `engine_ids` / `consecutive_failures`。
- 引擎恢复健康后任务自动解除暂停并在下一 tick 正常执行，`consecutive_failures` 归零。

## 现状（生产实测 + 代码根因）

- 生产 2026-11-23 03:05–04:04 CST：任务 83 每分钟失败一次，1 小时约 60 条 failed exec，
  `all offline (Issue #259)` 60–66 条/小时，`failed after 0 retries` 63–71 条/小时。
- `src/domains/runtime/engine/scheduler.py::_execute_autopilot_remote` 的两条引擎池路径
  （uid 池 / legacy id 池）在健康过滤为空后直接 `_update_execution_result(exec_id, "failed", ...)`
  并 `return`：不分类、不计数、不告警，且 `_execute_task` 之后无条件
  `_update_task_run_info(_compute_next_run(cron))` 推进 `next_run_at` → 形成
  「建 exec → 秒失败 → 下一 tick 再来一次」的空转出口缺失。
- 终态 `log.error(... failed after {max_retry} retries ...)` 不带失败原因与引擎标识，
  值班按 `level=ERROR` 抓不到「为什么失败」。
- 绑定 uid 在 `agent_runtimes` 中已不存在（引擎重注册换 uid）时，同样表现为每 tick 空转，
  没有任何重绑提示。

## 范围

In：

- 失败分类：健康过滤为空统一落 `error_code=ENGINE_OFFLINE`（uid 池与 legacy id 池同构）；
  绑定 uid/id 在 `agent_runtimes` 中**查无此行** → `error_code=BINDING_STALE`。
- 退避：`_compute_backoff_next_run(cron, n) = now + min(base·2^(n-1), 1800s)`，
  `base = max(cron 周期, 60s)`；退避窗口内调度器不派发（`backoff_until` 闸门），状态落库。
- 熔断：连续 3 次同因失败且（`ENGINE_OFFLINE`）离线持续 ≥ 5min → `circuit_paused=1`，
  发一次结构化告警（复用 `_notify_subscribers` 幂等通道）；`BINDING_STALE` 首次派发即告警
  （含重绑指引）。
- 自愈：绑定引擎恢复健康（或绑定 uid 重新存在）→ 清空失败状态并续跑。
- 可观测：终态 ERROR 单行结构化输出
  `task= exec= status= error_code= engine_ids= consecutive_failures= error=`，保留
  `all offline (Issue #259)` 指纹子串以兼容既有 Loki 查询；派发侧的 `all offline` WARNING
  并入该终态行，不再割裂。
- DDL：`autopilot_tasks` 增 5 列（`consecutive_failures` / `last_error_code` /
  `offline_since` / `backoff_until` / `circuit_paused`）；`autopilot_executions` 增
  `error_code`。新库走 schema DDL，存量库走登记式迁移（幂等 `_ensure_column` + `post_check`）。
- 测试：假时钟驱动调度裁决 + 执行收口，覆盖 1 小时断言（exec ≤ 4、告警 1 次、恢复续跑）。

Out：

- 引入 Prometheus 指标注册中心（仓库可观测走日志侧）。
- 引擎侧重注册换 uid 的根因治理（#3351）。
- 补跑引擎离线期间错过的时间窗（只保证恢复后下一个 tick 续跑）。
- #3741 / #3763 的同域收口改造。

## 验收

- [ ] 引擎持续离线 + 60s cron：1 小时新建 exec ≤ 4 条，`next_run_at` 呈 60/120/240/480s 退避序列。
- [ ] 连续同因 `ENGINE_OFFLINE` 熔断后任务 `circuit_paused=1`，告警**只发 1 次**且含
      `error_code` / `engine_ids` / `consecutive_failures`。
- [ ] 绑定 uid 缺失：首次派发即告警（含重绑指引）并退避，不再每 tick 建 exec。
- [ ] 终态 ERROR 单行含失败原因与 `engine_ids`，`all offline (Issue #259)` 指纹保留。
- [ ] 引擎恢复健康后任务自动解除暂停、`consecutive_failures` 归零并续跑。
- [ ] 假时钟测试覆盖上述 1h 断言；现有测试全绿。