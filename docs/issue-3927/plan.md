# Issue #3927 方案设计

## 1. 锁分层

新增 `src/kernel/lock_instrument.py`：

- `InstrumentedLock`：兼容 `RLock` 协议（可重入、`with`/`acquire`/`release`/`locked`）。
  等待/持有超过阈值（`DB_LOCK_SLOW_WAIT_SECONDS` 默认 2s、`DB_LOCK_SLOW_HOLD_SECONDS`
  默认 5s）时输出 `db_lock slow wait/hold` 警告，带 **acquire 时持锁栈**，
  并按 `(lock, owner)` 聚合指标。
- `lock_owner(label)`：ContextVar 标记 owner，日志/指标归因（如 `complete:t_xxx`、
  `health:stats:<rid>`）。
- `task_lock(task_id)`：任务级可重入锁 `t_<id>`。引用计数在获取前递增、释放后递减，
  归零即从登记表移除，避免「删除后重建导致双锁」竞态。

`session_manager.db_lock` 与 `owner_lock(domain)` 都替换为 `InstrumentedLock`，
保持原锁语义，仅增加可观测性。

## 2. complete 终态写拆分

`update_event_complete` 新增可选 `task_id`：

- `task_id` 非空 → 使用 `task_lock(task_id)`；否则沿用全局 `db_lock`（旧调用面零回归）。
- 锁内：SELECT 现状 → takeover/终态保护/复活窄口 → **只写终态字段**
  （status/exit_code/duration/completed_at/result/failed_node）并 commit。
- 锁外：`_write_event_payload_fields()` 序列化并写 `agent_logs`/`structured_output`。
  只有终态写者会走到这里；重复 complete 已在锁内短路，既幂等又不会重写大字段。
- 读侧兼容「终态先行、大字段补齐」；大字段写失败只记日志，不回滚终态。

`complete_task` / `fail_task` 传 `task_id`，使真实请求路径切到任务级锁。

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

- 同任务：任务锁串行；后到者看到终态直接返回。
- 跨任务：任务锁互不阻塞；MySQL 线程私有连接保证并行写安全。
- 任务锁获取顺序（任务锁 → 锁外大字段写 → 全局锁做 update_task_status）不存在
  锁序反转（任务锁在进入全局锁前已释放）。

## 6. 测试

`tests/runtime/test_3927_lock_scope.py`：

- 任务锁跨任务并行 / 同任务串行 / 用后回收；
- complete 接受 task_id 且拆写大字段；
- 重复 complete 幂等且不改首写大字段；
- 大字段写阻塞时全局锁可获取；
- refresh 聚合阻塞时全局锁可获取；
- 慢锁打印持锁栈 + owner 指标；
- A2A 异步开关与后台调度。

修复前缺失 `lock_instrument` / 拆写 seam / 锁外聚合，用例为红；修复后全绿。