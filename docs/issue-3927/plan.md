# Issue #3927 方案设计

## 1. 锁分层

新增 `src/kernel/lock_instrument.py`：

- `InstrumentedLock`：兼容 `RLock` 协议（可重入、`with`/`acquire`/`release`/`locked`）。
  等待/持锁超过阈值（`DB_LOCK_SLOW_WAIT_SECONDS` 默认 2s、`DB_LOCK_SLOW_HOLD_SECONDS`
  默认 5s）时输出 `db_lock slow wait/hold` 警告：慢持有带 acquire 时持锁栈
  （holder_stack）；慢等待带等待方栈（waiter_stack）+ **上一持锁方栈**
  （holder_stack(previous)），并按 `(lock, owner)` 聚合指标。
- `lock_owner(label)`：ContextVar 标记 owner，日志/指标归因（如 `complete:t_xxx`、
  `health:stats:<rid>`、`claim`、`update_task_status:<tid>`）。
- `task_lock(task_id)`：任务级可重入锁 `t_<task_id>`。引用计数在获取前递增、释放后递减，
  归零即从登记表移除，避免「删除后重建导致双锁」竞态。
- `event_lock(seq)`：事件终态保护锁 `t_ev:<seq>`（引用计数回收）。**审查 R1 修订**：
  终态保护按事件维度统一——传/不传 task_id 的全部终态写者
  （complete/fail/判死 `_sync_event_failed`/接管/redispatch 重置）共用一把锁，
  `SELECT status → UPDATE` 恢复原子；task_id 仅作归因与同任务附加串行。

`session_manager.db_lock` 与 `owner_lock(domain)` 都替换为 `InstrumentedLock`，
保持原锁语义，仅增加可观测性。

**指标基数有界（审查 R3 修订）**：实体锁指标锁名用粗粒度常量（`task_lock` /
`event_lock`）；owner 尾段实体 id（task uuid / seq / runtime id）在**指标与
Prometheus 标签层**折叠为 `prefix:*`（日志仍打印完整 owner）；`(lock, owner)`
条目数受 `LOCK_METRICS_MAX_ENTRIES`（默认 512）约束，超限折叠进
`(lock, "__overflow__")` 桶——长稳进程指标字典不再无界增长。

## 2. complete 终态写拆分

`update_event_complete` 新增可选 `task_id`（审查 R1 修订后语义）：

- 终态保护统一 `event_lock(seq)`（`t_ev:<seq>`）；`task_id` 非空时额外嵌套
  `task_lock(task_id)` 同任务串行。**不再**按「传 task_id 走任务锁 / 不传走全局
  db_lock」分裂口径——那会让同一 seq 的两类写者互不可见，`SELECT → UPDATE`
  失去原子性。
- 锁内（`_write_guard → event_lock → task_lock`，固定锁序，无交叉死锁面）：
  SELECT 现状 → takeover/终态保护/复活窄口 → **只写终态字段**并 commit；
  随后 `agent_logs/structured_output` 大字段在同一 event_lock 内短写
  （审查 R2 修订：不再游离在保护锁外；拆事务保留「终态先行、大字段补齐」）。
- **payload 失败自愈（审查 R2 修订）**：写失败登记 `_pending_payload` 标记；
  重复 complete 在终态保护处短路时若发现 pending 且带 payload → 补写一次并清
  标记。消除「事件 completed 而 structured_output 永久为空、下游 A2A 载荷缺字段」
  的不可自愈路径。
- **SQLite/单库短临界区（审查 R4 修订）**：`_write_guard()` 按后端选择——MySQL
  （每线程/池化连接，commit 只作用于本线程语句）`nullcontext()` 无锁；SQLite/
  单库共享连接（commit 是**连接级**语义，会把其他线程未提交语句一并提交）保留
  `db_lock` 短临界区，防跨线程事务交错。
- `reset_event_terminal`（redispatch）同样纳入 `event_lock(seq)`：终态解除与
  迟到终态写互斥（锁序 db_lock → event_lock 与 complete 一致）。

`complete_task` / `fail_task` 传 `task_id`，使真实请求路径归因到任务维度。

## 2b. update_task_status 大字段拆分（审查 R5 修订）

claim 等待的是 `session_manager.db_lock`，而 complete 路径原本在锁内写全量
`result_text/usage_json`（可达数十 MB）+ 级联——若 17–50s 排队命中 db_lock，
事件侧拆锁对 claim 无收益。`update_task_status` 拆为：

- db_lock 短临界区：终态判定 + status/completed_at/exit_code 小写 + 级联收敛 + commit；
- 大字段（result_text/usage_json/errors/stderr）：锁释放后单条 UPDATE 短写，
  MySQL 免锁、SQLite/单库保留短临界区（同 `_write_guard` 口径），失败只记日志
  （事件侧 structured_output 为权威产出且有 pending 自愈兜底）。
- 同任务串行由 db_lock 短临界区 + 单条原子 UPDATE 保证；终态幂等判定在锁内完成，
  只有终态赢家会写大字段。

**灰度验证步骤（发布前必做）**：
1. 用本 PR 的慢锁埋点观察灰度实例：`db_lock slow wait/hold lock=db_lock
   owner=claim|claim_by_tag` 的 wait 秒数与 `lock=owner:runtime owner=complete:*`
   的 hold 秒数（Loki 查 `db_lock slow`）。
2. 判定 17–50s 命中哪把锁：若 claim 的 wait 主要落在 `lock=db_lock` 且
   holder_stack 指向 `update_task_status/complete_task` 大字段写 → 本节拆分生效
   后应显著下降；若仍命中其它 db_lock 持有方（chat 路由等），按同一模式另行拆分。
3. `pending_payload_count()` 应长期为 0；非 0 说明 payload 短写持续失败，需排查。

**回滚步骤**：异常时 revert 本 PR 即回到基线锁语义（无独立开关）；灰度期可先设
`COMPLETE_A2A_ASYNC=0` 关闭异步推进，再评估是否整 PR 回退（≤15 分钟内可完成）。

## 3. refresh 快照读 + 锁外聚合

`health_scorer._fetch_task_stats` 拆为：

- `_snapshot_task_rows()`：取窗口内终态行（status, completed_at）+ 最近一条状态。
  SQLite 单连接仍用 `_lock()` 串行；MySQL 用线程自有连接，快照读不持进程锁。
- `_aggregate_task_rows()`：纯函数，锁外计算 total/failed/completed/
  failed_effective/consecutive_failures/last_failure_at。口径与原 SQL 一致
  （连败最多看 50 条、衰减窗口清零、shutdown 自愈逻辑不变）。
- `_persist_score()`：只保留一次短 UPDATE（健康分写回）。
- `_refresh_all_loop` 仍逐引擎处理；启动时取 id 列表为一次短锁。

## 4. A2A 异步化（灰度）

`complete_task` 的 auto-advance 通过 `_schedule_auto_advance_after_complete`：

- 默认同步（保持既有 stage 幂等与测试确定性）；
- `COMPLETE_A2A_ASYNC=1` → 派发在后台 daemon 线程执行，不占用 complete 请求线程。

A2A 派发本身早已在 `db_lock` 释放后执行，本改动进一步把「锁外但请求内」的长尾
移出请求线程。

## 5. 幂等与并发安全

- 同事件：event_lock 串行全部终态写者；后到者看到终态直接返回（幂等短路）。
- 同任务：task_lock 附加串行；update_task_status 的终态判定在 db_lock 内完成。
- 跨任务/跨事件：互不阻塞；MySQL 线程私有连接保证并行写安全。
- 锁序固定：`db_lock(SQLite guard) → event_lock → task_lock`，任何路径不反向
  嵌套，无交叉死锁面；终态/大字段写均不持 session 全局锁（MySQL）。
- **拆写一致性约束（审查 R2）**：终态先行、大字段补齐期间，读侧（事件列表/
  A2A 载荷）必须容忍 payload 短暂为空；payload 失败必须登记 pending 并在下次
  重复 complete 短路时补写——不允许「终态落库 + payload 永久为空」。
- **混合锁口径约束（审查 R1）**：任何新增的事件终态写路径都必须经
  `update_event_complete`（或至少 `event_lock(seq)`），禁止绕过事件锁直写
  events.status 终态值；redispatch 重置经 `reset_event_terminal`。

## 6. 测试

`tests/runtime/test_3927_lock_scope.py`：

- 任务锁跨任务并行 / 同任务串行 / 用后回收；
- complete 接受 task_id 且拆写大字段；
- 重复 complete 幂等且不改首写大字段；
- 大字段写阻塞时全局锁可获取（MySQL 拓扑）；
- 混合锁口径：带/不带 task_id 的终态写者同 seq 互斥（R1）；
- payload 失败 pending 自愈补写（R2）；
- SQLite 拓扑 payload 写保留短临界区（R4）；
- 并发 3 complete + 1 claim：claim 的 db_lock 不随 complete 线性排队
  （spec 验收第 1 条，update_task_status 大字段拆分口径）；
- refresh 聚合阻塞时全局锁可获取；
- 慢锁打印持锁栈 + waiter/holder(previous) 栈区分 + owner 指标；
- 指标粗化（task_lock/event_lock + owner 折叠）与容量上限（R3）；
- A2A 异步开关与后台调度。

修复前缺失 `lock_instrument` / 事件锁 / 拆写 seam / pending 自愈 / 短临界区
guard，用例为红；修复后全绿。

## 7. CI 门禁（审查-受控执行 Required）

`.github/workflows/lock-regression.yml`：push/PR 触发
`tests/runtime/test_3927_lock_scope.py` + `tests/kernel/test_owner_db.py`
必跑门禁，锁并发回归不再只靠人工执行。