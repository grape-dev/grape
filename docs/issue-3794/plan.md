# Plan: Issue #3794

## 方案：两段短事务分批 + 注册式组合索引 + 1205/1213 重试 + 指标

### 切片 1：组合索引（db_setup，**注册式迁移**）
- QA #1981 C1 校准：不能写进老轨 `_create_indexes()`——老轨只在「版本表为空首跑」或
  「执行器异常兜底」时执行，存量库（已盖 legacy-stamp 202610300001）启动时整段不跑，
  三库模式下 runtime 域进程只跑登记迁移，更是永远到不了那段 → 1205 根因在生产不消除。
- 改为 `register_migration(version="202611240009", name="add-agent-tasks-event-seq-status-index",
  kind="safe", domains=("runtime",), fn=_fn_add_agent_tasks_event_seq_status_index)`：
  - fn 在 runtime 域连接上执行 `CREATE INDEX IF NOT EXISTS idx_agent_tasks_event_seq_status ON agent_tasks(event_seq, status)`；
  - 同一迁移回收左前缀冗余索引 `idx_agent_tasks_event`（组合索引已覆盖，热表少维护一棵树；
    MySQL DROP 带 `ON agent_tasks`，SQLite 不带；删不掉只告警不阻断索引补齐）；
  - 建索引失败必须抛出（不盖章，下次启动幂等重试），吞掉会「版本号盖死、热表仍逐行扫」。
- MySQL 显式 `status(191)` 并吞 1061 重复索引、SQLite 保持 `CREATE INDEX IF NOT EXISTS`：三库拓扑下迁移走 `db_registry._CompatCursor`，mysql_compat 的「自动补 (191)」特判只挂在其自身 Cursor 上、此处不生效，写裸列名在 MySQL 会 1064/1170 失败且永不盖章。
- 守卫：`tests/api/test_migration_track_guard.py` 新增 `LEGACY_INDEX_DDL_FROZEN` +
  `test_legacy_index_ddl_is_frozen`——往老轨塞补索引会让它变红（补列/补表守卫的姊妹）。

### 切片 2：purge_stale_event_guards 重写（event_store.py）
- 常量：`_PURGE_BATCH = 200`、`_PURGE_MAX_ROUNDS = 50`、`_LOCK_RETRY_BACKOFF_SECONDS = 0.2`、
  `_RETRYABLE_LOCK_ERROR_CODES = (1205, 1213)`。
- 第一段（只读短事务）：`SELECT seq FROM event_active_guard WHERE created_at < ? AND seq > ?
  ORDER BY seq LIMIT 200`——不含相关子查询，不与热表 agent_tasks 互锁；空集退出。
- **keyset 推进（QA #1981 R2）**：每轮 `last_seq = max(seqs)`，无论本批删掉多少都严格后移。
  旧实现 `deleted == 0 → break` 时，候选批恰好全被 running/dispatched 保护就整轮结束，
  剩余可删过期守卫永不再清理（200 保护 + 5 可删 → 两次调用均返回 0、205 行残留）。
  空转只受 `_PURGE_MAX_ROUNDS` 上限约束。
- 第二段（删除短事务）：`DELETE ... WHERE created_at < ? AND seq IN (...) AND NOT EXISTS
  (SELECT 1 FROM agent_tasks t WHERE t.status IN ('dispatched','running') AND
  t.event_seq = event_active_guard.seq)`——DELETE 内重新复核，防 select→delete 之间
  task 转 running 的 TOCTOU（§33 不回归）。每批独立 `db_lock` + commit。
- 锁冲突容错：`_is_retryable_lock_error(e)`（`e.args[0] ∈ {1205,1213}` 优先，message 含
  `Lock wait timeout` / `deadlock` 兜底）→ 退避 0.2s + 随机抖动重试一次；**重试前
  `db.rollback()`**（失败语句的事务与行锁未释放时同事务重试会再撞锁，且延长对
  claim/heartbeat 的持锁时间）；仍失败 WARNING 带批序号与 seq 集合，返回累计已删数。
- 结束条件：空集 / 本批行数 < BATCH / 达到 50 轮上限。

### 切片 3：指标
- Counter `grape_event_guard_purged_total`（label `result`，挂 `src.domains/insight/observability`
  的独立 registry）；`result="ok"` **按删除行数计**（每批 rowcount 成功即 inc），`result="failed"` **每条失败计 1**；
  逐批计 ok 保证后续轮次失败提前返回时已删行数不丢失；
  prometheus_client 不可用时静默跳过（模块级缓存 Counter，避免重复注册）。
- `log.info` 输出 `purged=N`。

### 测试
`tests/runtime/test_issue_3794_guard_purge.py`（11 例，seed 参照 `test_event_active_guard.py`）：
1. `test_expired_guard_without_running_task_is_purged`：过期 guard（无运行中 task）被删。
2. `test_fresh_guard_is_not_purged`：未过期 guard 不删。
3. `test_guard_with_running_task_is_not_purged`：过期 guard + running task 不被删（§33）。
4. `test_guard_with_dispatched_task_is_not_purged`：dispatched task 同样保护（§33）。
5. `test_guard_with_terminal_task_is_purged`：终态 task 不算运行中，guard 可删。
6. `test_more_than_batch_size_rows_are_all_purged`：450 行（>2×BATCH）过期 guard 全部删完，返回总数。
7. `test_protected_batch_does_not_block_remaining_purgeable_guards`：200 受保护 + 5 可删 → 一次调用删 5 条（keyset 头阻塞回归，QA #1981 R2）。
8. `test_lock_wait_timeout_1205_retried_once`：DELETE 首次抛 1205 → 重试一次成功照常删除（mock `_delete_guard_batch`）。
9. `test_lock_wait_timeout_1205_retry_exhausted_returns_zero`：重试仍 1205 → WARNING 口径返回 0、不抛出。
10. `test_deadlock_1213_also_retried_once`：DELETE 首次抛 1213 deadlock → 同样重试一次成功。
11. `test_batch_constant_within_contract`：分批契约常量 BATCH=200 / 最多 50 轮。

`tests/api/test_issue_3794_event_seq_status_index.py`（5 例）：迁移登记（version=202611240009/kind/domains/fn）+
fn 建组合索引并回收冗余索引 + fn 幂等 + MySQL DDL 契约
（`test_mysql_ddl_uses_text_prefix_and_avoids_if_not_exists`：无 `IF NOT EXISTS`、含 `status(191)`）+
老轨 `_create_indexes()` 不含该索引。
`tests/api/test_migration_track_guard.py`：老轨索引 DDL 冻结守卫（`test_legacy_index_ddl_is_frozen`）。
CI：`.github/workflows/windows-smoke.yml` linux-backend 门禁追加上述三文件。

## DDL 落点校准（QA #1981 经验沉淀）
本仓 DDL 有两条轨，落错轨不报错、只是永远不执行：

| 轨道 | 何时执行 | 允许放什么 |
|---|---|---|
| 老轨 `_run_all_migrations()` / `_create_indexes()` | 版本表为空首跑、执行器异常兜底 | **只减不增**（已冻结） |
| 注册式 `register_migration(version, name, kind, domains=..., fn=...)` | 每次启动按版本表增量执行 | 补列 / 补表 / 补索引 / 回填 |

判据：新增补索引一律 `register_migration`，`domains` 填**表属主域**（`agent_tasks` → runtime），
fn 内用 `_migration_conn(属主域)` 取连接、失败抛出（不盖章下次重试）。
守卫：`test_legacy_track_is_frozen`（迁移调用清单）+ `test_legacy_index_ddl_is_frozen`（索引名清单）。

## 版本号与基线同步（QA #1981 复审 C1）

分支已 rebase 到 `arch-weekly-11-ljt` @ `03f9e51`。上一轮把 #3733 顺延 006、#3928 顺延 007 的
「基线撞号修复」已全部撤销（`db_setup.py` 注册/函数名、`backend_health_columns.py`、
`tests/kernel/test_issue3733_autopilot_dispatch_state_migration.py`、`docs/issue-3733/plan.md`、
`docs/issue-3928/{plan,task}.md` 均恢复基线口径）——其它 Issue 的号由基线自己维护，不随本 PR 改动。
本迁移按基线实际占用（`202611240004`~`202611240008`）顺延到下一个空闲号 `202611240009`，
`tests/arch/test_migration_version_integrity.py` R1 唯一性在合并后仍成立。

## 提交拆分
1. `fix(#3794): purge stale event guards in two short batched transactions with lock-timeout retry`
2. `feat(#3794): register (event_seq,status) index migration 202611240009`
3. `ci(#3794): gate guard purge and index migration tests in linux backend`
4. `docs(#3794): document migration 202611240009 and the baseline avoidance chain`