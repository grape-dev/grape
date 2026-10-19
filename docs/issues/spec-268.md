# Spec: Issue #268 — latency cleanup SQLite datetime() 语法在 MySQL 后端失效修复

## Objective
修复 `src/api/routes/latency.py` `_maybe_cleanup()` 中 SQLite 专有 `datetime('now', '-N days')` 语法，使 `api_latency_log` 定时清理在 MySQL 后端正常生效。

## Background
- `src/api/routes/latency.py:31` 使用 `datetime('now', '-7 days')`，这是 SQLite 专有语法。
- `src/mysql_compat.py` 的 `translate_sql` 仅翻译 `datetime('now')`（正则 `_COL_DATETIME_DEFAULT_RE`），不匹配 `datetime('now', '-N days')`，导致原样透传到 MySQL → 1064 语法错误。
- 影响：超期记录删除从未执行；超量裁剪（`_MAX_ROWS`）因第一条 DELETE 抛异常被跳过；`api_latency_log` 表无限增长。
- 同类 bug 还存在于 `src/api/db_setup.py:2009` `_cleanup_old_latency_logs()`（启动时执行），同样会触发 1064 错误，违反「MySQL 后端启动后不再出现 1064 语法错误日志」验收标准。

## Scope

### In Scope
- `src/api/routes/latency.py` `_maybe_cleanup()` 超期记录 DELETE 语句按后端分支适配
- `src/api/db_setup.py` `_cleanup_old_latency_logs()` 同一 bug 修复（启动时执行，必须修复以满足启动无 1064 错误的验收标准）

### Out of Scope
- `_maybe_cleanup()` 超量裁剪 `DELETE ... WHERE id IN (SELECT ... ORDER BY id ASC LIMIT n)`（MySQL 8 可用，无需修改）
- 其他 route 文件中的 `datetime('now', '-N days')` 用法（如 dashboard.py，属不同功能域）
- 数据库 schema 变更、数据迁移

## Technical Approach
方案 A — 后端分支适配（与仓库 `src/user/db.py:2687-2689` 已有惯例一致）：
- MySQL：`NOW() - INTERVAL N DAY`
- SQLite：保留原 `datetime('now', '-N days')`

通过 `from src.mysql_compat import get_db_backend` + `is_mysql = get_db_backend() == "mysql"` 判断后端。

## Acceptance Criteria
- [ ] MySQL 后端启动后不再出现 1064 语法错误日志
- [ ] 超期记录删除（保留 7 天）正常执行
- [ ] 超量裁剪（`_MAX_ROWS`）逻辑不再因前序异常被跳过
- [ ] SQLite 后端向后兼容性保持
- [ ] 所有现有测试通过
