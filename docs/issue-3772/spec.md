# Spec: Issue #3772 — MySQLCompatCursor 迭代协议缺失，no_progress 扫描恒失败

## Objective

修复 `_sweep_no_progress_tasks()` 在 MySQL 后端扫描第一步即抛
`TypeError: 'MySQLCompatCursor' object is not iterable` 的缺陷，恢复无进展检测与
`NO_PROGRESS_REQUEUE` 自动回炉整链路；同时让「扫描停摆」可观测，不再静默 `return 0`。

成功标准：

- `MySQLCompatCursor` 迭代/上下文语义与 `sqlite3.Cursor` 一致；
- `_sweep_no_progress_tasks()` 在 MySQL 后端不再因 `list(cursor)` 失败；
- 全仓同类 `list(<cursor>.execute(...))` 写法核验完毕；
- 扫描失败有 `grape_no_progress_scan_failures_total` 计数指标；
- 连续 5 次失败升级 ERROR 日志并由 `deploy/alerts.yml` 规则告警；
- 双后端迭代一致性单测落在 `tests/kernel/test_mysql_compat.py`。

## In Scope

- `src/kernel/mysql_compat.py`：`MySQLCompatCursor` 补 `__iter__` / `__next__` /
  `__enter__` / `__exit__`。
- `src/domains/runtime/engine/scheduler.py`：`_sweep_no_progress_tasks()` 扫描调用点改
  `c.execute(...)` + `c.fetchall()`。
- 扫描失败可观测：新增指标定义（insight observability）+ public 转发口 + scheduler 失败分支
  计数、连续失败阈值日志；`deploy/alerts.yml` 新增规则。
- `tests/kernel/test_mysql_compat.py`：追加 MySQL/sqlite 双后端迭代一致性用例。
- `tests/runtime/test_issue_3772_no_progress_scan.py`：扫描调用点不依赖隐式迭代与失败计数回归。

## Out of Scope

- #3741（Autopilot 独占引擎）、#3353（认领后不重建会话）本体修复。
- #3738（meta upsert rowcount）本体修复。
- MySQL 连接池、方言改写等其它兼容层行为变更。

## Technical Approach

1. 兼容层：`__iter__` 返回 cursor 自身，`__next__` 基于 `fetchone()`（无行时
   `StopIteration`），与 `sqlite3.Cursor` 行为一致，同时兼容 `_pycur` 流式与 `_rows`
   预取两条路径；`__enter__` 返回自身，`__exit__` 调 `close()`。
2. 调用点：扫描显式 `c.execute(...)` 后 `rows = c.fetchall()`，不再依赖隐式迭代协议；
   其余 `list(cursor.execute(...))` 全仓核验（当前仅此一处直依赖）。
3. 可观测：`grape_no_progress_scan_failures_total` Counter（无高基数标签）；scheduler
   模块级连续失败计数，成功清零；达到 5 次时 `log.error` 一次，指标持续累加。
   `deploy/alerts.yml` 规则 `increase(...[5m]) >= 5` 覆盖「约 5 分钟连续失败」。

## Acceptance Criteria

- [ ] `list(cur.execute("SELECT 1")) == [(1,)]`；`for row in cur` 与 `cur.fetchall()`
      结果一致；`with db.cursor() as cur` 正常关闭。
- [ ] SQLite 基准与 MySQL 兼容层迭代结果一致（`tests/kernel/test_mysql_compat.py`）。
- [ ] `_sweep_no_progress_tasks()` 不再调用 cursor 的隐式迭代（回归用例覆盖）。
- [ ] 扫描失败指标可查；连续 5 次失败 ERROR/告警规则落地。
- [ ] 既有 `test_mysql_compat*.py`、`test_issue_2793_no_progress.py` 无回归。

## Open Questions

- 无。线上 WARNING 归零、4 台主控生产验证依赖 weekly-11 发布批次回归，不在代码仓库内完成。