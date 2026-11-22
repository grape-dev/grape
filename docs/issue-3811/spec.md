# Issue #3811 Spec：release guard 按域读取任务 Issue

## 目标

三库拓扑（mgmt/runtime/event）下，`src/domains/runtime/vcs/release_guard.py` 的
`_read_task_issue_id()` 按域路由读取 runtime 域表 `agent_tasks`，消除
`Table 'grape_prod_mgmt.agent_tasks' doesn't exist`（1146），恢复「任务 Issue 与写入目标
Issue 一致」守卫的拦截能力，并让守卫失效（fail-open）可观测。

## 现状

- `_read_task_issue_id()` 的 MySQL 兜底分支用进程主库 `sm.db` + 裸 SQL
  `SELECT issue_id FROM agent_tasks`，无 schema 前缀；三库拓扑下 mgmt/worker 进程主库为
  `grape_prod_mgmt`，恒 1146。
- SQLite 只读快路径被 `if db_path` 跳过（MySQL 后端 `db_path` 为空），MySQL 分支成为唯一
  路径且必然失败。
- `resolve_session_task_issue()` 返回 None → `check_issue_consistency()` fail-open → 会话
  token 写入的 Issue 一致性守卫 100% 失效；同时 1,737 条/日 WARNING 淹没告警面。

## 改动

1. MySQL 分支按域取连接，复刻 `src/domains/contracts/local/runtime_local.py` 范式：
   `db_registry.domain_db("runtime")` / `domain_lock("runtime")` / `t_runtime("agent_tasks")`。
2. 回落 `sm.db` 时调用 `db_registry.warn_if_cross_domain_fallback("runtime")`，使降级可见。
3. 保留 SQLite 只读快路径。
4. `except` 分支的 `return None` 增加 fail-open 计数 + WARNING（含 session_key、异常摘要、
   backend 拓扑），守卫策略本身不变（仍 fail-open 放行）。

## 验收标准

- 三库拓扑下会话 token 触发 git-repo 写操作不再出现 `Table 'grape_prod_mgmt.agent_tasks'
  doesn't exist`。
- 单测：runtime 有自有 DSN 时命中 runtime 库；无自有 DSN 时回落 `sm.db` 并打降级告警。
- 回归：任务 Issue ≠ 目标 Issue 时仍返回 409，不退化为恒 fail-open。
- 现有 release guard 相关测试全绿。
- Loki 验收（sre）：修复发布后 1 小时
  `{service_name="grape-mgmt"} |~ "release guard: resolve task issue failed"` 计数为 0。

## 范围外

- 同族 #3771/#3763/#3732/#3798 的其它跨域裸 SQL 落点。
- 一致性判定策略、表结构/迁移、新连接池/新依赖。