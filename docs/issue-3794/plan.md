# Plan: Issue #3794

## 方案：两段短事务分批 + 注册式组合索引 + 1205/1213 重试 + 指标

### 切片 1：组合索引（db_setup，**注册式迁移**）
- QA #1981 C1 校准：不能写进老轨 `_create_indexes()`——老轨只在「版本表为空首跑」或
  「执行器异常兜底」时执行，存量库（已盖 legacy-stamp 202610300001）启动时整段不跑，
  三库模式下 runtime 域进程只跑登记迁移，更是永远到不了那段 → 1205 根因在生产不消除。
- 改为 `register_migration(version="202611240003", name="add-agent-tasks-event-seq-status-index",
  kind="safe", domains=("runtime",), fn=_fn_add_agent_tasks_event_seq_status_index)`：
  - fn 在 runtime 域连接上执行 `CREATE INDEX IF NOT EXISTS idx_agent_tasks_event_seq_status ON agent_tasks(event_seq, status)`；
  - 同一迁移回收左前缀冗余索引 `idx_agent_tasks_event`（组合索引已覆盖，热表少维护一棵树；
    MySQL DROP 带 `ON agent_tasks`，SQLite 不带；删不掉只告警不阻断索引补齐）；
  - 建索引失败必须抛出（不盖章，下次启动幂等重试），吞掉会「版本号盖死、热表仍逐行扫」。
- 只写 SQLite 通用语法；MySQL TEXT 前缀由 mysql_compat 在 CREATE INDEX 特判自动补 (191)。
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
  claim/heartbeat 的持锁时间）；仍失败 WARNING 只带批序号与 `seqs_n` / `seqs_range`（不打印整批 seq，避免撞锁频繁时日志爆炸），返回累计已删数。
- 结束条件：空集 / 本批行数 < BATCH / 达到 50 轮上限。

### 切片 3：指标
- Counter `grape_event_guard_purged_total`（label `result`，挂 `src.domains/insight/observability`
  的独立 registry）；`result="ok"` **按删除行数计**（每批 rowcount 成功即 inc），`result="failed"` **每条失败计 1**；
  逐批计 ok 保证后续轮次失败提前返回时已删行数不丢失；
  prometheus_client 不可用时静默跳过（模块级缓存 Counter，避免重复注册）。
- `log.info` 输出 `purged=N`。

### 测试
`tests/runtime/test_issue_3794_guard_purge.py`（seed 参照 `test_event_active_guard.py`）：
1. 过期 guard（无运行中 task）被删。
2. 过期 guard + 运行中 task（event_seq=guard.seq）不被删（§33）。
3. >200 行过期 guard 全部删完（分批路径，返回总数）。
4. 200 受保护 + 5 可删 → 一次调用删 5 条（keyset 头阻塞回归，QA #1981 R2）。
5. DELETE 首次抛 1205 → 重试一次成功照常删除（mock `_delete_guard_batch` side_effect）。
6. DELETE 首次抛 1213 deadlock → 同样重试一次成功。
7. 重试仍 1205 → WARNING 口径返回 0、不抛出。

`tests/api/test_issue_3794_event_seq_status_index.py`：迁移登记（version/kind/domains/fn）+
fn 幂等（建组合索引 + 回收冗余索引）+ 老轨不含该索引。
`tests/api/test_migration_track_guard.py`：老轨索引 DDL 冻结守卫。
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

## 提交拆分
1. `test(#3794): guard TTL 分批清理先写失败测试`（红）
2. `fix(#3794): purge_stale_event_guards 两段短事务分批 + 1205 重试 + 组合索引 + 指标`（绿）
3. `test(#3794): 受保护候选批头阻塞与 1213 deadlock 重试 先写失败测试`（红，QA #1981 R2/M1）
4. `fix(#3794): 候选窗口按 seq keyset 推进 + 1205 重试前 rollback 并纳入 1213`（绿）
5. `fix(#3794): 组合索引改注册式迁移 + 回收左前缀冗余索引`（C1/M3）
6. `test(#3794): 迁移落点与用例接入 linux-backend 门禁`（R3/经验沉淀）
7. `docs(#3794): spec/plan/task 对齐审查修改`（交付文档）