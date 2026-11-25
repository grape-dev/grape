# Issue #3877 方案设计

## 1. 补列：登记式迁移（存量库唯一有效路径）

新增 `src/domains/runtime/migrations/events_cascade_columns.py`：

- `TABLE = "events"`，`COLUMNS` = 五列（列定义与 `schema_ddl._EVENTS_TABLE_DDL` 逐字一致）：
  `TEXT DEFAULT ''` ×4 + `INTEGER DEFAULT 0`。
- `add_events_cascade_columns(conn=None)`：
  - 取 `session_manager.db` + `db_lock`（与 202611240006 / 202611240008 同口径）；
  - `_table_present` / `_is_base_table` 不满足 → 抛 `db_setup._NonOwnerObject`
    （三库下非 owner 库只看到跨库只读视图，不能 ALTER，执行器「跳过且不盖章」）；
  - 逐列 `db_setup._ensure_column(c, "events", col, ddl, strict=True)`：幂等，
    MySQL 侧 TEXT DEFAULT 由 `ddl_safety` 剥默认值走 INSTANT，不触发 COPY 重建。
- `verify_events_cascade_columns(conn=None)`：`PRAGMA table_info` / `SHOW COLUMNS` 探列
  （`ddl_safety.table_columns` 双方言），缺任意一列抛 `RuntimeError` → 不盖章、下次启动重试。

`src/api/db_setup.py` **字面量**登记（不能用 `**_MIGRATION`，否则
`test_migration_track_guard.py` 的版本号解析守卫看不见）：

```python
register_migration(version="202611250001", name="events-cascade-compensation-columns",
                   kind="safe", domains=("runtime",), fn=..., post_check=...)
```

新装库仍由 `schema_ddl` 直接带列；`ALTERS_SESSION_EVENT_COLUMNS` 里的五条保留不动
（legacy 全量首跑的建库顺序依赖，且 `test_legacy_track_is_frozen` 只冻结调用名，不改常量）。

## 2. 写侧：列存在性过滤

`service._cascade_event_terminal_impl`：

- `_events_columns()`：`_table_columns("events")`（runtime 句柄 `_rt_db`，进程内缓存，
  MySQL 走 `SHOW COLUMNS`），**探列失败返回空集 = 未知** → 按全列写（保持历史行为）。
- `_build_cascade_terminal_update(cols, values)`：按实际列集裁剪 SET 子句
  （终态 5 列 + cascade 5 列，`structured_output` 仍为 `COALESCE(?, structured_output)`），
  返回 `(sql, params, missing_cascade_cols)`；一列都写不了时抛
  `RuntimeError("... no writable columns ...")`（明确失败，不发空 SET）。
- **1054 兜底重试**：UPDATE 抛「1054 / Unknown column / no such column」→ 失效
  `_TABLE_COLS_CACHE["events"]` 重探一次列集，按新列集重建再执行一次（应对并发迁移/缓存漂移）。

## 3. guard 释放：finally

```
try:
    终态 UPDATE（含 1054 重试）
except: 结构化日志 + 指标; return False
finally:
    _release_event_active_guard(cursor, seq, triggered_by, task_status)
```

`_release_event_active_guard` 自带 try/except：失败时结构化日志 + 计数器
（`stage=guard`），绝不把成功的收敛吞成异常。

## 4. 可观测性：kernel 收集器 + 结构化日志

新增 `src/kernel/cascade_converge_metrics.py`（与 `src/kernel/comment_metrics.py` 同口径：
kernel 不 import domains，`install(registry)` 由 insight 域注册，未安装时 observe 为 no-op）：

- `grape_event_cascade_converge_failed_total{reason,stage}`：reason ∈
  `unknown_column / no_writable_columns / db_error / unknown`；stage ∈ `prepare / update / guard`。
- `grape_event_cascade_converge_degraded_total{missing}`：`missing` 为缺失列名逗号串（限枚举 5 列，基数可控）。

日志（Loki 查询面）：

- 失败：`_cascade_event_terminal: failed for event <seq> -> <status> | stage=... reason=... missing_columns=... | error=...`（ERROR；
  保留既有 `|= "_cascade_event_terminal: failed"` 查询前缀）。
- 降级：`_cascade_event_terminal: degraded for event <seq> -> <status> | missing_columns=... | wrote only existing columns`（WARNING）。

## 5. 存量补偿入口

`service.reconverge_terminal_gaps(seqs=None, limit=200)`：查「事件仍活跃 + 任务已终态」
的 (seq, task_id)（同一 seq 多条重试任务取最新一条），逐个走
`converge_event_to_task()`（`_real` 回填路径，`triggered_by=converge_event`）。
幂等：已终态事件返回 `noop`，可重复执行。

## 6. 回归用例

- `tests/api/test_issue_3877_events_cascade_columns_migration.py`
  （生产同款执行器 `_run_migrations_with_conn` + sqlite 内存库）：已盖章存量库补列并盖章 /
  幂等 / 半列不盖章 / 非 owner 视图跳过不盖章 / 五列不得回退进冻结老轨常量。
- `tests/runtime/test_issue_3877_cascade_events_columns.py`：
  正常写五列 + guard 删除；缺列降级（只写存在列、终态收敛、guard 仍释放）；
  1054 缓存失效重试；结构化日志前缀 + 指标计数；`reconverge_terminal_gaps` 幂等补偿。