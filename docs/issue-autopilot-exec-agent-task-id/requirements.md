# 需求：autopilot 执行记录按 event_seq 撞号误判终态，改为直持 agent_task_id

- 单号：待平台分配
- 基线：`origin/weekly-10`（`40be0820a`），本地 `weekly-mys` 已对齐该提交
- 缺陷类型：跨序列表误关联 → 真任务结果被终态幂等丢弃（数据丢失 + 监控失真）

## 背景

### 现象

自动化（autopilot）执行记录 `status=success` 但 `result` 为空，而对应任务实际已跑完并产出完整结果。全库 3385 条执行记录中 1205 条为「成功但空结果」；某每日定时任务连续三晚复现；其中「exec 终态时间早于真任务创建时间」的 1217 条，命中七个定时任务（单任务最多 306 条）。

### 已核实证据（生产快照，以预发复核为准）

- exec 3721：`23:00:24.447` 开始，`23:00:24.515` 即写终态（68ms），而真任务 `23:00:24.619` 才创建。
- 同日日志顺序：`enqueuing` → `zombie recovery: exec 3721 -> success` → `enqueued task_id=...`——回收器抢在真任务落库前判定。
- 被匹配的是两个月前一条 `event_seq` 同号的 `a2a_event` 任务（completed、result_text 空）。`events.seq` 与 `autopilot_executions.id` 是两个独立序列，同号纯属巧合。
- 真任务 result_text 内容完整，晚到约 9 分钟，被 `_update_execution_result` 的终态幂等丢弃。

### 已核实根因（基线 `origin/weekly-10` 逐函数核对，行号仅参考、一律以函数名锚定）

1. `src/runtime/scheduler.py::scheduler_loop` 同一轮先 `asyncio.ensure_future(_execute_task(task))` 异步派发，紧接着同步调用 `_reap_zombie_executions()`——派发协程尚未执行到 `enqueue_task` 落库，回收器已经开跑。
2. `_reap_zombie_executions` 用 `SELECT ... FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1` 反查任务；`event_seq` 写的是 `autopilot_executions.id`（`enqueue_task(..., event_seq=exec_id)`）。该查询**无 `event_type` 判别、无时间边界、无年轻度保护**，直接命中历史同号行。
3. 命中历史 `completed` 行后按 `_update_execution_result(exec_id, "success", result=<历史行 result_text>)` 收口，历史行 result_text 为空 → 写入 `success + 空结果`。
4. `_update_execution_result` 有终态幂等（已 success/failed/cancelled 不再覆盖），真任务 9 分钟后回来写结果时被丢弃。
5. 同类反查在自动化为四处：`_poll_dispatched_completion`（按 event_seq 查状态）、`_reap_zombie_executions` 内两处（状态 + runtime/last_seen 离线判死）、`src/api/routes/autopilot.py` 执行列表展示反查。

### 关键事实（避免误改）

- `agent_tasks` 的 `_TASK_COLUMNS` 与表结构**不得**改动；`event_type='autopilot_event'` 是派生任务的可判别特征。
- `_update_execution_result` 的终态幂等是「用户 cancelled 后迟到回写不得洗状态」的保护，**不得改动语义**。
- 迁移执行器语义（`src/api/db_setup.py::_run_migrations_with_conn`）：fn 正常返回即盖章；fn 抛错则不盖章、下次启动幂等重试（`kind='safe'` 亦如此）；`legacy-stamp` 首跑路径与存量盖章路径必须都生效。

## 改动范围

严格 5 组共 12 项；多改一处即打回。

### A 组：表结构与迁移（2 项）

- A1 `autopilot_executions` 增加列 `agent_task_id TEXT DEFAULT NULL`（存 `agent_tasks.id`，非 `events.seq`）；不新增其它列，不改 `agent_tasks` 任何列。
- A2 新增迁移 `register_migration(version="202611060001", name="add-autopilot-exec-agent-task-id", kind="safe", fn=_fn_add_ap_exec_agent_task_id)`：版本号不得复用（必须大于现有 `202611020002`）；fn 内 `with db_lock` + `_ensure_column(c, "autopilot_executions", "agent_task_id", "TEXT DEFAULT NULL")` + `CREATE INDEX IF NOT EXISTS idx_ap_exec_agent_task ON autopilot_executions(agent_task_id)`（MySQL 由 `MySQLCompatDB` 改写，按既有 `_index_sqls` 写法）；fn 必须幂等（列/索引已存在即正常返回并盖章），失败抛出交由执行器跳过不盖章。

### B 组：写入直连 id（2 项）

- B1 `_execute_task` 中 `task_id = enqueue_task(...)` 成功后，立即 `UPDATE autopilot_executions SET agent_task_id=? WHERE id=?`（`str(task_id)`，`exec_id`），位置在 `log.info("... enqueued task_id=...")` 之前、同一 `try` 块内；失败只 `log.warning`，不改变派发主流程。
- B2 入队失败分支（`_update_execution_result(exec_id, "failed", error="入队失败")`）保持 `agent_task_id` 为空，不做伪造填充。

### C 组：四处反查改为按 agent_task_id（3 项）

- C1 新增模块内辅助 `_get_exec_agent_task(c, exec_row_or_id)`：`agent_task_id` 非空时按 `SELECT ... FROM agent_tasks WHERE id=? LIMIT 1`；为空（历史行）才走兜底 `WHERE event_seq=? AND event_type='autopilot_event' AND created_at >= <exec.started_at>`；`started_at` 缺省时用 `created_at`；时间比较须解析为 **aware datetime**（`datetime.fromisoformat`，无时区按 UTC 补齐），解析失败视为不匹配；两路都无 → 返回 `None`（调用方 `continue`，禁止退回裸 `event_seq`）。
- C2 三处调用改为 C1：`_poll_dispatched_completion` 的状态查询、`_reap_zombie_executions` 的状态查询与 runtime/last_seen 查询（离线判死那处）。除查询来源外，后续状态映射、evidence 校验、pending_self_report 不判死等逻辑一律不动。
- C3 `src/api/routes/autopilot.py` 执行列表的展示反查同口径（agent_task_id 优先 + C1 兜底），不得再裸 `event_seq`；响应字段 `agent_task_id` 语义不变（仍是字符串，空为 `""`）。

### D 组：年轻度保护（1 项）

- D1 `_reap_zombie_executions` 对 `started_at`（空则 `created_at`，均空则跳过）距当前 < 120 秒的执行直接 `continue`；常量命名 `ZOMBIE_YOUNG_GRACE_SECONDS = 120` 放在模块常量区；时区解析规则同 C1。

### E 组：测试（4 项）

- E1 `tests/test_autopilot_exec_agent_task_id.py`（sqlite 内存库、不压生产）：
  1. 同号场景——历史 `a2a_event` 任务（completed、result_text 空）+ 随后创建的真 `autopilot_event` 任务 → 回收器跑一轮后 exec 仍为 `running/dispatched`，不得写 success；
  2. 直连场景——exec 的 `agent_task_id` 指向真任务 → 反查命中真任务（用桩断言未执行 `event_seq` 查询）；
  3. 年轻度——`started_at=now` 的 exec 即便有历史同号行也不收口；
  4. 兜底边界——历史 exec（`agent_task_id` 为空）+ `autopilot_event` 但 `created_at` 早于 `started_at` 的同号行 → 不匹配。
- E2 迁移测试：存量库（`grape_t_schema_migrations` 已盖章）启动后本迁移盖章一次、列与索引存在；再次运行 0 pending；新装库（空版本表）首跑后列与索引同样存在。
- E3 回归：`_update_execution_result` 终态幂等用例、deploy evidence 收口、`pending_self_report` 不判死、`_sweep_pending_self_report_timeouts` 既有用例全绿。
- E4 日志断言：启动与派发日志无 `agent_tasks INSERT 缺少列值`（`_TASK_COLUMNS` 未动的间接验证）。

## 明确禁止

1. 不改 `agent_tasks` 的 `_TASK_COLUMNS`、表结构、任何列语义。
2. 不改 `_update_execution_result` 的终态幂等语义与 `completed_at` 写法。
3. 不动 `events.py` / `event_pipeline.py` / `runtime/service.py` 等**真实事件**路径上的 `event_seq` 反查（它们查的是 `events.seq`，与本缺陷无关）。
4. 不批量重写存量 1205 条「成功但空结果」执行记录，不改历史 `agent_tasks` 行（如需修数据另开单）。
5. 不改前端、不改 HTTP 响应字段、不新增依赖、不顺手重构、不改无关日志级别。
6. 不移除 `_reap_zombie_executions` 的既有保护（5min 离线宽限、pending_self_report 不判死、部署 evidence 校验）。
7. **不 push、不建 MR、不部署、不重启服务**；只提交到本地 `weekly-mys` 分支并标注「待人工审批」。

## 验收标准

1. 静态核对：`git diff` 只含 A/B/C/D/E 列出的文件（`runtime/scheduler.py`、`api/routes/autopilot.py`、`api/db_setup.py`、新增测试），改动量在预估区间。
2. 单测：同号历史 `a2a_event` + 稍后创建的真任务 → 回收后 exec 仍为 running/dispatched（E1-1）。
3. 单测：`agent_task_id` 已写入时反查只按 `id` 取行，且 `event_seq` 查询未被触发（E1-2）。
4. 现网手动触发一次定时任务 → 该 exec 的 `result` 与 `agent_task_id` 均非空，`status=success`。
5. 连续两晚 cron 后校验 SQL 返回 0：`e.result='' AND e.status='success' AND t.created_at > e.completed_at`（`e`=autopilot_executions，`t`=其 agent_task）。
6. 迁移在存量库与新装库均生效、只盖章一次、二次启动 0 pending；日志无 `agent_tasks INSERT 缺少列值`、无 `checksum 漂移`。
7. 回归：终态幂等、deploy evidence、pending_self_report、离线判死宽限等既有用例全绿；`agent_tasks` 表结构无变化（列清单对拍）。

## 参考资料

- 派发链路：`_execute_task`（`_create_execution` → `enqueue_task(event_seq=exec_id, event_type="autopilot_event")` → `_poll_dispatched_completion`）。
- 回收链路：`scheduler_loop` 每轮末尾 `_reap_zombie_executions()` → 命中即 `_update_execution_result(...)`。
- 迁移写法规避：`_ensure_column` 已含 MySQL INSTANT→INPLACE 降级与 MDL 超时转人工清单逻辑，直接复用，不要自己写裸 `ALTER TABLE`。
- 规模预估：生产代码约 110–150 行（A 组 ~30、B 组 ~10、C 组 ~60、D 组 ~10），测试约 180–220 行。
- 测试口径：sqlite 内存库 + 时间/DB 打桩；不连生产库、不触发远端执行。
- 数据口径：3385 / 1205 / 1217 / 306 均为生产历史快照，执行前以预发复核为准。