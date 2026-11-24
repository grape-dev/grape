# Issue #3811 方案

## 连接路由

`_read_task_issue_id()` 分三条路径：

1. SQLite 只读快路径（现状保留）：非 MySQL 后端且 `_db_path` 存在时用 `mode=ro` 短连接。
2. 域路由：`own = db_registry.uses_own_dsn("runtime")`；`db = domain_db("runtime") or sm.db`；
   `lock = domain_lock("runtime") if own else sm.db_lock`；
   `table = "agent_tasks" if own else t_runtime("agent_tasks")`。
3. 异库回落：`own == False` 时调用 `db_registry.warn_if_cross_domain_fallback("runtime")`，
   单库/同域拓扑静默（库名相同），三库缺 DSN 时每进程每域告警一次。

表名只来自常量或 `t_runtime` 白名单，参数仍走占位符，不引入注入面。

## fail-open 可观测

模块级 `_FAIL_OPEN_STATS` 计数；查询异常时 `return None` 前记录一次 WARNING
（`release guard: task issue lookup fail-open (guard disabled) count=N backend=...`），
与新告警文案区分于 Loki 验收的旧 `resolve task issue failed`。

## 测试 seam

- seam = `release_guard._read_task_issue_id(session_key)` 公开读函数。
- sqlite 双库 fixture：mgmt 库放错值、runtime 库放对值，断言有 DSN 时读 runtime、无 DSN 时读
  mgmt 并打 `[db-guard]` 降级告警。
- 409 回归沿用 `check_issue_consistency` / endpoint 现有 seam。

## 风险

runtime 无自有 DSN（单库/双库）时回落取错连接：以 `uses_own_dsn` 决定 lock 与表名归属，
回落显式告警且保留 fail-open。表名拼接注入：表名仅常量/白名单，占位符传参。