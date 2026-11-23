# Issue #3927 实现 Spec：全局 db_lock 长持有治理

> 来源：Issue #3927 Spec（评论 id=203098262）| 目标分支 `arch-weekly-11-ljt` | 里程碑 arch-weekly-11

## 目标

把进程级单锁（`session_manager.db_lock` / `owner_lock("runtime")`）的长持有（17–50s）
收窄为任务级短临界区，使 complete 终态写与后台 `_refresh_all_loop` 不再双向堵锁，
claim / claim-by-tag / heartbeat-v2 / tags 的排队等待回到秒级。

## 范围（本 PR 落地）

1. **锁分层**：新增任务级锁 `t_<task_id>`（引用计数回收，跨任务并行、同任务串行）；
   complete/fail 终态写切到任务级锁，未传 task_id 的旧调用面保持全局锁。
2. **终态写拆分**：`update_event_complete` 锁内只写终态字段（单条 UPDATE），
   `agent_logs` / `structured_output` 大字段独立短写且不持全局锁。
3. **refresh 改造**：`_fetch_task_stats` 改为「快照读 + 锁外聚合」，
   COUNT/SUM 等聚合从进程锁内移到 `_aggregate_task_rows`；MySQL 下快照读亦不持锁。
4. **A2A 异步化（灰度开关）**：complete 的 auto-advance 派发可经
   `COMPLETE_A2A_ASYNC=1` 卸载到后台线程；默认同步以保持既有 stage 幂等语义。
5. **慢锁可观测**：`InstrumentedLock` 在等待/持有超阈值时打印**持锁栈**，
   并按 `(lock, owner)` 维度记录指标（`lock_metrics_snapshot()`，可选 Prometheus 计数器）。
6. **并发幂等**：同一任务重复 complete 在任务锁内先做终态保护，快速返回，
   不再重复写大字段。

## 非目标

- #3688（清理 DELETE / 大字段读）、#3813（巡检扫描）、#3794（守卫 TTL）的独有逻辑；
- 数据库引擎替换 / 分库分表 / 连接池重构；管理台与前端改动。

## 验收

- 并发 3 complete + 1 claim：claim 不随 complete 线性增长（新增并发回归用例守护）。
- 同一任务重复 complete 幂等快速返回，不覆盖首写终态/大字段。
- 大字段写阻塞时全局 `db_lock` 仍可立即获取（锁内只剩终态 UPDATE）。
- refresh 聚合计算期间全局锁可立即获取（锁外聚合）。
- 慢锁超阈值打印持锁栈 + owner 指标可查。
- 现有相关测试全绿；新增用例在修复前为红。