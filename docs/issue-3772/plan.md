# Plan: Issue #3772

## 分层与改动点

| 层 | 文件 | 改动 |
|----|------|------|
| kernel（底层兼容） | `src/kernel/mysql_compat.py` | `MySQLCompatCursor` 补 `__iter__`/`__next__`/`__enter__`/`__exit__` |
| runtime（调用点） | `src/domains/runtime/engine/scheduler.py` | 扫描改用 `fetchall()`；失败计数 + 连续失败 ERROR |
| insight（指标） | `src/domains/insight/observability/__init__.py` | 新增 `grape_no_progress_scan_failures_total` Counter 与 record 函数 |
| insight（跨域门面） | `src/domains/insight/public/__init__.py` | 转发 `record_no_progress_scan_failure` |
| deploy（告警） | `deploy/alerts.yml` | 新增 `GrapeNoProgressScanFailing` 规则 |
| tests | `tests/kernel/test_mysql_compat.py` | 双后端迭代/上下文一致性 |
| tests | `tests/runtime/test_issue_3772_no_progress_scan.py` | 扫描不依赖隐式迭代 + 失败计数/连续失败 |

## 关键设计决策

1. **`__iter__` 返回 self + `__next__` 走 `fetchone()`**：与 `sqlite3.Cursor` 语义一致，
   同时覆盖兼容层两条取数路径（`_pycur` 流式、`_rows` 预取）。相较
   `iter(self.fetchall())`，不会在 `list(cursor)` 与 `fetchone()` 混用时产生两份消费语义。
2. **调用点仍显式 `fetchall()`**：即使兼容层已补齐协议，热点扫描路径不依赖隐式协议，
   与仓内既有 MySQL 兼容写法对齐（双保险）。
3. **指标无标签**：失败原因可能是异常类型，作为标签会带来高基数；异常类型进日志即可。
4. **连续失败阈值 5**：扫描周期 60s，连续 5 次约 5 分钟，避开瞬时抖动；达到阈值仅
   升级一次 `log.error`，之后仍按 warning + 计数，避免刷屏。

## Seam（测试边界）

- kernel seam：公开 `MySQLCompatCursor` 的迭代/上下文协议，通过 mock 裸 pymysql cursor
  构造相同结果，与 `sqlite3.Cursor` 对照。
- runtime seam：`_sweep_no_progress_tasks(now_epoch=...)` 公开函数；用非可迭代假 cursor
  证明扫描路径不依赖 `__iter__`。
- 指标 seam：`record_no_progress_scan_failure()` 公开函数 + 模块级计数器。

## 风险

| 风险 | 缓解 |
|------|------|
| 迭代协议影响其它 MySQL 路径 | 只新增 dunder，未改既有 fetch 语义；全仓核验 + 回归 |
| 指标跨域 import 违规 | scheduler 用函数内 `from src.domains.insight.public import ...`（仓内既有模式），R4 为 report-only |
| worker 包无 prometheus | 指标仅主控侧 scheduler 记录；public 转发函数异常静默，不影响扫描 |