# Plan: Issue #536 — 数据库迁移版本化与 DDL 安全带

## 代码考察结论（关键事实）

- `_run_all_migrations()`（db_setup.py:2555）：init_database() 唯一迁移入口，SQLite/MySQL 共用；133 处 ALTER 集中在 `_migrate_tables()`（:3568）的 migrations 列表与散落各处的内联 DDL；
- 迁移锁：`init_database()`（:2710）MySQL 分支已用 `GET_LOCK` + 池外专用连接（`db.dedicated_connection()`，MySQLCompatDB:837），锁名按库隔离——**原样复用，不改**；
- meta 表：db_setup._META_TABLE_DDL（:127）`key TEXT PRIMARY KEY, value TEXT`；读写走 `src/user/db.py get_meta_value/set_meta_value`（:2830/:2842，`INSERT OR REPLACE`）；
- 列检查：`PRAGMA table_info` 由 mysql_compat 翻译为 information_schema 查询，双后端共用；
- 危险 DDL 调用点（MySQL/SQLite 双跑）：`_rename_column_if_exists`（:1769，内含 RENAME+DROP）、`_migrate_tables` 内 5 处内联 DROP COLUMN（skill.current_version :3647、repo_im_group_binding.account :3931、devops_agent_session.vod_bot_app_id :4123）、内联 RENAME COLUMN（:3937、:4148、:4153、:4158）、1 处 MySQL 专用 MODIFY（grape_accounts.email :1696）；
- `_ensure_column`（:1759）：PRAGMA 判存后 `ALTER TABLE ADD COLUMN`，无 ALGORITHM 声明；
- 版本号：`src/worker_package.py worker_code_version()`（"h-{manifest 指纹}"）——主控进程可用作自身代码版本；但 worker 闭包 manifest 不含 src/api/db_setup.py 之外的诸多服务端文件，作为 **master 自身版本**改用 `src/worker_package._manifest_version` 不可靠。**决策：新增 `src/api/code_version.py` 的 `current_code_version()`**（git commit 优先，回退 manifest 指纹），min_code_version 比较用简单字符串前缀即可（同源同格式）；
- 测试基建：tests/conftest.py 会话级重建 data/test_sessions.db 并跑 init_database()；tests/atomic/ 为 Issue 级红/绿契约测试先例；pytest.ini 有 `atomic` marker；
- pre-commit：仓库无 .pre-commit-config.yaml；已有 scripts/check-*.py + scripts/check-all.sh 检查器家族——**CI/pre-commit 卡点以 `scripts/check-destructive-ddl.py` + 挂入 check-all.sh + 文档说明 pre-commit 接入方式落地**。

## 设计决策（对齐设计文档 D1–D4）

| 决策 | 选择 | 理由 |
|------|------|------|
| checksum 不匹配 | log.warning 不阻断 | fail-safe，避免启动被卡死 |
| backfill 失败 | 跳过不阻断启动 | 回填可重试，可用性优先 |
| min_code_version 存放 | meta 表 | 与 DB 状态同源，多节点读到同一值 |
| DROP COLUMN 圈定 | 调用点（5 处内联 + _rename_column_if_exists 内 1 处） | 代码无 `_drop_column_if_exists` 函数 |
| 自身版本号 | 新增 src/api/code_version.py | worker manifest 指纹不含服务端全部文件 |
| legacy-stamp 内容 | 打包 `_run_all_migrations` 现有 12 个顶层函数整体 | 函数体一行不动，行为等价于现状 |

## 改动清单（M1→M4）

### M1 版本表 + 注册表执行器 + legacy-stamp（src/api/db_setup.py）

1. `_SCHEMA_MIGRATIONS_DDL`：version VARCHAR(32) PK / name VARCHAR(128) / kind VARCHAR(16) / applied_at DATETIME / duration_ms INT / checksum VARCHAR(64)，挂入 `_create_tables()` 的 DDL 列表（全新库自然建出）；
2. `Migration` dataclass + `_MIGRATIONS` 注册表（列表，version 升序）+ `register_migration()`；
3. 执行器 `run_pending_migrations()`：
   - 读版本表（表不存在=未初始化 → legacy-stamp 路径）；
   - legacy-stamp 路径：检测「表新建且无任何盖章行」→ 先照旧执行 `_run_all_migrations()`（行为与现状完全一致），成功后 INSERT 一条 `version=<legacy-stamp 版本>、kind=legacy-stamp` 盖帽；失败不盖章（下次重跑，幂等兜底）；
   - 增量路径：只执行未盖章项，逐条盖章（duration_ms 实测、checksum=fn 源码 sha256[:16]）；
   - 已盖章但 checksum 变化 → log.warning 不阻断；
4. `init_database()` 各分支在 `_run_all_migrations()` 调用点**替换**为 `run_pending_migrations()`（内部决定全量 or 增量），锁机制不动；
5. 语义：`_run_all_migrations()` 函数体一行不动，仅调用方切换。

### M2 DDL 安全带（src/api/db_setup.py）

1. `_ensure_column()`：MySQL 后端时 ALTER 语句拼 `ALGORITHM=INSTANT`；异常（不支持 INSTANT）→ 重试 `ALGORITHM=INPLACE, LOCK=NONE`；再失败 → log.error 进人工清单（模块级 `_DDL_MANUAL_REVIEW` 列表 + 启动汇总 ERROR 日志），**不静默 COPY、不抛出阻断**（列未建成，下次启动重试）；
2. 迁移专用连接会话锁超时：`init_database()` MySQL 分支拿锁成功后 `SET SESSION lock_wait_timeout=10`；MDL 超时错误（Lock wait timeout / 1205）在执行器捕获 → log.error 附 `_query_mdl_holders()` processlist 摘要 → 跳过不盖章。

### M3 危险 DDL 收敛 + min_code_version 门槛 + CI 卡点

1. `_destructive_ddl(c, ddl, *, kind_hint)`：统一执行入口——校验 cleanup 上下文（当前处于 kind=cleanup 迁移执行中，模块级 `_ACTIVE_MIGRATION_KIND` 上下文标记）+ MySQL 侧 `min_code_version` 门槛（`_cleanup_gate_passed()`：meta 无 key 或自身版本 ≥ min_code_version 才放行）；不满足 → log.info 跳过不盖章（下版本再试）；
2. 6 处危险 DDL 调用点改走 `_destructive_ddl()`（`_rename_column_if_exists` 的 RENAME/DROP 分支 + 5 处内联 DROP；内联 RENAME 已在 `_rename_column_if_exists` 语义覆盖内的保持原样，其余直调）；MODIFY（grape_accounts.email）也收口；
3. meta 门槛函数：`get_min_code_version()` / `set_min_code_version()`（复用 meta 表）；
4. `scripts/check-destructive-ddl.py`：对 `origin/$WORK_BRANCH...HEAD` 的 db_setup.py diff，新出现的 DROP/RENAME/MODIFY COLUMN 行必须出现在 `_destructive_ddl(` 调用行或 cleanup 声明块（`_MIGRATIONS` 中 kind="cleanup" 条目）内；挂入 scripts/check-all.sh。

### M4 验收演练

- tests/atomic/test_issue_536_migrations.py：版本表盖章/幂等（二次 0 pending）、checksum 告警、backfill 分批+失败跳过、cleanup 门槛、_ensure_column 降级链、lock_wait_timeout 下发、legacy-stamp 首跑；
- 400 万行表 + 蓝绿 v1/v2 共存演练属测试库手工验收（Issue 验收标准 3/4），代码侧以契约测试覆盖行为语义。

## 测试 seam 声明

- 公开 seam：`run_pending_migrations()`（幂等/盖章/分流）、`_ensure_column()`（ALTER 语句形态，MySQL mock）、`_destructive_ddl()`（门槛判定）、`get/set_min_code_version()`；
- mock 方式：tests/mock_db.py 的 MockCursor/MockDB 注册 SQL 匹配器断言下发语句；不连真实 MySQL。

## 上线顺序（不变量）

dev 验证 → 测试库演练（400 万行表 + 蓝绿共存）→ 双主升级（最后一次旧方式同时停机）→ 之后新迁移一律走注册表。
