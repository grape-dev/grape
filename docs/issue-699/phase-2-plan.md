# Phase 2 Plan — 实施方案（Issue #699）

技术方案（按 spec §2 决策展开）：

## 1. src/kernel/db.py（新建）

```
SCHEMA_MGMT / SCHEMA_RUNTIME / SCHEMA_INSIGHT        # 常量
TABLE_REGISTRY: dict[str, str]                        # 85+ 表 → schema
SQLITE_PREFIX = {grape_mgmt: mgmt_, grape_runtime: rt_, grape_insight: ins_}
table(schema, name) -> str                            # 前缀/限定
t_mgmt/t_runtime/t_insight(name) -> str
get_db_backend() -> str                               # 复用 mysql_compat（惰性 import 防 R5 环）
schema_mode_enabled() -> bool                         # GRAPE_SCHEMA_MODE
qualify_sql(sql) -> str                               # 运行时表名改写（跳过字面量；仅表名语法位置）
ensure_schemas(conn)                                  # MySQL CREATE SCHEMA
migrate_sqlite_prefixes(conn) / migrate_mysql_schemas(conn)
SchemaSQLiteDB / SchemaSQLiteCursor                   # SQLite 连接代理（execute 入口改写）
```

qualify_sql 实现：
- `_split_literals(sql)`：把 SQL 按 `'...'` 字面量切段，只对非字面量段做替换；
- 替换正则：`\b(FROM|JOIN|INTO|UPDATE|TABLE|(?:RENAME\s+TO))\s+(?:IF\s+NOT\s+EXISTS\s+)?\b(name)\b`（大小写不敏感），name 命中注册表才替换；
- 未启用 schema mode 或注册表未命中 → 原样返回。

## 2. 挂接点

- `src/kernel/session_manager.py`：sqlite 分支创建 `db` 后，若 `schema_mode_enabled()` 则包一层 `SchemaSQLiteDB`（MySQL 分支由 mysql_compat 内部处理）。代理透传 commit/rollback/cursor/execute/executemany/executescript/PRAGMA 等；游标属性（lastrowid/rowcount/description）经 `__getattr__` 透传。
- `src/kernel/mysql_compat.py`：`MySQLCompatDB.execute/executemany` 及内部 Cursor.execute 在翻译前先过 `qualify_sql`（仅 schema mode 开启时）。

## 3. contracts 空壳（src/domains/contracts/）

- `mgmt_service.py` / `runtime_service.py` / `insight_service.py`：ABC，方法签名取自 design §5/§7（get_repo_webhook_config / get_agent_session / get_scene_config / verify_session_token / get_bot_config / get_runtimes / get_tasks / get_events / get_task_messages / log_obs 等），docstring 标注 Phase 5 填充。

## 4. 脚本

- `scripts/migrate_schemas.py`：幂等迁移；`--backend sqlite|mysql`、`--dry-run`；SQLite 用 `ALTER TABLE ... RENAME TO`；MySQL `CREATE SCHEMA IF NOT EXISTS` + `RENAME TABLE`；迁移记录写 `grape_t_schema_migrations`。
- `scripts/replace_table_refs.py`：codemod（通道 B）——把指定文件中 SQL 字符串里表名语法位置的裸表名替换为 `{t_xxx('name')}` f-string 插值并补 import；`--check` 模式输出统计；默认不整仓执行。

## 5. 测试（tests/test_kernel_db.py + tests/contracts/）

- TC-2.1：sqlite/mysql 两后端 t_* 断言（monkeypatch env）。
- TABLE_REGISTRY 完整性：85 张全部有归属；3 schema 计数（mgmt≥48、runtime=22、insight=11）。
- qualify_sql：正常 SELECT/INSERT/UPDATE/DDL 改写；列名 agent_type 不改写；字符串字面量内容不改写；已前缀名不改写。
- e2e（TC-2.3）：子进程 `DEVOPS_DB_PATH=<tmp> GRAPE_SCHEMA_MODE=1` import session_manager → 建表带前缀，插入/查询读写正常。
- TC-2.6：contracts 三接口 import。
- migrate_schemas.py：临时 sqlite 库 rename 幂等 + dry-run。
- 回归：全量 pytest + scripts/check-arch.py（R1/R2/R6 红线不新增）。

## 6. 原子提交序列

1. `docs: #699 add Phase 2 spec, plan and task breakdown`
2. `feat: #699 add kernel/db.py schema registry and SQL qualify layer`
3. `feat: #699 hook schema mode into session_manager and mysql_compat`
4. `feat: #699 add contracts service interface shells`
5. `feat: #699 add schema migration and table-ref codemod scripts`
6. `test: #699 add kernel db, contracts and schema e2e tests`
