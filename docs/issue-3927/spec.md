# Issue #3927 实现 Spec：全局 db_lock 长持有治理

> 来源：Issue #3927 Spec（评论 id=203098262）| 目标分支 `arch-weekly-11-ljt` | 里程碑 arch-weekly-11

## 目标

把进程级单锁（`session_manager.db_lock` / `owner_lock("runtime")`）的长持有（17–50s）
收窄为任务级短临界区，使 complete 终态写与后台 `_refresh_all_loop` 不再双向堵锁，
claim / claim-by-tag / heartbeat-v2 / tags 的排队等待回到秒级。

## 范围（本 PR 落地）

1. **锁分层**：新增任务级锁 `t_<task_id>` 与**事件终态保护锁 `t_ev:<seq>`**
   （均为引用计数回收，跨任务/跨事件并行）；complete/fail 终态写与判死/回收/
   接管等全部终态写者统一按事件维度取锁（审查 R1），`SELECT status → UPDATE`
   恢复原子；task_id 仅作归因与同任务附加串行。
2. **终态写拆分**：`update_event_complete` 锁内只写终态字段（单条 UPDATE），
   `agent_logs` / `structured_output` 大字段在同一事件锁内拆事务短写且不持
   全局锁（审查 R2）；payload 写失败登记 pending，重复 complete 短路时自愈补写。
   MySQL（每线程/池化连接）无锁并行；SQLite/单库共享连接保留 `_write_guard`
   短临界区防跨线程事务交错（审查 R4）。
3. **update_task_status 大字段拆分（审查 R5）**：claim 等待的 `session db_lock`
   短临界区只剩终态判定 + status/completed_at/exit_code 小写 + 级联；
   result_text/usage_json/errors/stderr 大字段锁释放后单条 UPDATE 短写。
   claim / claim_by_tag / update_task_status 加 `lock_owner` 归因埋点，
   灰度前据此确认 17–50s 命中哪把锁（验证步骤见 plan.md §2b）。
4. **refresh 改造**：`_fetch_task_stats` 改为「快照读 + 锁外聚合」，
   COUNT/SUM 等聚合从进程锁内移到 `_aggregate_task_rows`；MySQL 下快照读亦不持锁。
5. **A2A 异步化（灰度开关）**：complete 的 auto-advance 派发可经
   `COMPLETE_A2A_ASYNC=1` 卸载到后台线程；默认同步以保持既有 stage 幂等语义。
6. **慢锁可观测**：`InstrumentedLock` 在等待/持有超阈值时打印持锁栈；慢等待
   区分输出 waiter_stack 与 holder_stack(previous)（上一持锁方栈）；
   按 `(lock, owner)` 维度记录指标（可选 Prometheus 计数器）。指标基数有界
   （审查 R3）：实体锁名粗粒度（task_lock/event_lock）、owner 尾段实体 id 折叠
   为 `prefix:*`、条目数受 `LOCK_METRICS_MAX_ENTRIES` 上限约束。
7. **并发幂等**：同一任务重复 complete 在事件锁内先做终态保护，快速返回，
   不覆盖首写终态/大字段；payload 缺失时可自愈补写。
8. **CI 门禁**：`lock-regression` workflow 在 push/PR 必跑并发回归用例。

## 非目标

- #3688（清理 DELETE / 大字段读）、#3813（巡检扫描）、#3794（守卫 TTL）的独有逻辑；
- 数据库引擎替换 / 分库分表 / 连接池重构；管理台与前端改动。

## 验收

- 并发 3 complete + 1 claim：claim 不随 complete 线性增长（update_task_status
  大字段拆分口径，并发回归用例守护）。
- 混合锁口径：传/不传 task_id 的终态写者对同一 seq 互斥，首写终态不被覆盖。
- 同一任务重复 complete 幂等快速返回，不覆盖首写终态/大字段；payload 首写失败
  时重复 complete 可自愈补写。
- 大字段写阻塞时全局 `db_lock` 仍可立即获取（MySQL 拓扑；SQLite/单库保留
  `_write_guard` 短临界区，其内只有终态/payload 短 UPDATE）。
- refresh 聚合计算期间全局锁可立即获取（锁外聚合）。
- 慢锁超阈值打印持锁栈；慢等待区分 waiter_stack / holder_stack(previous)；
  owner 指标可查且条目有界（粗化 + `LOCK_METRICS_MAX_ENTRIES` 上限）。
- 现有相关测试全绿；新增用例在修复前为红（CI `lock-regression` 门禁必跑）。