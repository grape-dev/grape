# Plan: Issue #3826

## 方案概述

把「issue 域走哪条连接」的判定收敛到内核 `db_registry.route_of("runtime")` 单一事实源：
`_DomainDbProxy` 删除 `uses_own_dsn ? DSN : session_manager.db` 的自有回落，三库拓扑缺
`GRAPE_DSN_RUNTIME` 时显式 `DomainUnavailable`；同时把 COUNT 失败的 WARNING 扩成
「label + 异常 + route/target_db/pid/host」，让部署问题与代码问题在日志层可分流。

## 为什么不再新增原语

`db_registry.route_of` / `cursor_for` / `new_connection` 已存在且明确「禁止各自回落」；
本次是「迁移漏点复用既有原语」，只补一个脱敏的 `database_of(domain)`（DSN path 取库名）
供日志与 `expected_db_name` 复用，避免第二份 DSN 解析。

## 实现步骤

1. `src/kernel/db_registry.py`
   - 新增 `database_of(domain) -> str`：`dsn_of(domain)` → `parse_dsn(...)["database"]`，
     缺 DSN/解析失败返回 `""`；不含 user/password。
   - `expected_db_name(role)` 改为 `database_of(domain)`（行为逐字等价，消除重复解析）。
2. `src/domains/runtime/routes/issues.py`
   - `_DomainDbProxy._route()`：`try: db_registry.route_of("runtime")`，
     `DomainUnavailable` → `None` + `log.debug`。
   - `cursor()`：`route_of("runtime")`（**不 catch**，`DomainUnavailable` 上抛）；`domain` →
     `new_connection("runtime")` + `_OwnedCursor`；`local` → `session_manager.db.cursor()`。
   - `commit()/rollback()`：`route == "domain"` → `None`（域连接 autocommit）/ 域连接 rollback；
     `local` → `session_manager.db.commit()/rollback()`；`None`（不可用）→ no-op（debug）。
   - 新增 `_count_failure_context()`：`route=... target_db=... pid=... host=...`。
   - `_count_issues` 的 `except` 追加该 context（`label` 前缀保持）。
3. 测试 `tests/runtime/test_issue_3826_count_no_fallback.py`（新）
   - A：三库拓扑缺 runtime DSN → `_rt_db.cursor()` 抛 `DomainUnavailable`，`session_manager.db`
     哨兵零触达；`_count_issues` 返回 `None`；`my_issues(per_page=20)` 的 `total is None`、
     满页 `has_more=True`。
   - B：日志字段 —— 域路由用例断言 `route=domain target_db=grape_prod_runtime pid=<os.getpid()>
     host=<hostname>` 且不含 `pwd`/口令；local 拓扑断言 `route=local target_db=<DEVOPS_DB_NAME>`；
     缺 DSN 断言 `route=unavailable`。
   - C：棘轮 —— `_DomainDbProxy` 源码含 `route_of("runtime")`、不含 `uses_own_dsn`；
     `issues.py` 内无裸 `db.cursor()`。
   - D：审计 —— `src/` 全量 `COUNT(*) FROM issue` 仅允许 3 处白名单（路由层 1 处 + 迁移探测 +
     runtime public 原语），路由层必须在 `_count_issues` 内。
4. `tests/runtime/test_issue_3771_mine_count_domain.py`：`_patch_mgmt_topology` 由
   `current_domain/uses_own_dsn/new_connection` 迁到 `lib_enabled/current_domain/dsn_of/
   new_connection`（真实走 `route_of`），保持既有 11 条断言语义不变。
5. `.github/workflows/windows-smoke.yml`：已接入的「Issues mine COUNT domain routing tests
   (issue #3771)」步骤 `run` 追加 #3826 用例文件（同一步门禁，不新增 job）。

## 测试 seam

公开 seam = `GET /api/issues/mine` 处理函数 `my_issues(...)` + `db_registry` 三库拓扑
（先例 `tests/runtime/test_3350_runtime_domain_db_routing.py`、`test_issue_3771_*`）；
`_rt_db.cursor()` 作为代理契约 seam 直接断言，不测 SQL 语义。

## 红/绿拆分

1. `test(#3826)`：新用例 + 3771 seam 迁移（对当前实现必红：代理仍回落、日志无字段）。
2. `feat(#3826)`：`db_registry.database_of` + `_DomainDbProxy` 收口 + 日志字段（转绿）。
3. `ci(#3826)`：门禁步骤追加用例。

## 风险与回归

- `_rt_db` 在 `issues.py` 有 40+ 调用点：只改连接判定，不改 SQL/提交语义；`commit/rollback`
  的 local 分支与旧实现逐字一致。
- `DomainUnavailable` 上抛会让此前「静默成功」的跨域读变成显式失败——这正是规格要求
  （与 `test_domain_route_strict.py` S1–S8 同一契约）。
- 回归面：`tests/runtime/test_domain_proxy_commit_2524.py`、`test_report_rollback_2524.py`、
  `test_pipeline_title_backfill_2925.py`、`test_pipeline_window_derived_alias.py`、
  `test_issue_3771_mine_count_domain.py`、`tests/kernel/test_domain_route_strict.py`、
  `tests/kernel/test_domain_db.py`、`tests/runtime/test_issues_mine_paging.py`。