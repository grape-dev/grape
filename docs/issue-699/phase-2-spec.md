# Phase 2 Spec — 分 Schema + 表前缀管理（Issue #699）

> 依据：docs/weekly-09/grape-unified-implementation-design.md §4（分 Schema 设计）、§12 Phase 2、§14 TC-2.1~TC-2.7。

## 1. 需求（What）

在 Phase 1（kernel/ 提取 + domains/ 域内聚，PR !836）基础上，实现数据库层的分 Schema 管理：

1. `src/kernel/db.py` — schema 前缀管理：
   - 常量 `SCHEMA_MGMT="grape_mgmt"` / `SCHEMA_RUNTIME="grape_runtime"` / `SCHEMA_INSIGHT="grape_insight"`
   - `table(schema, name)`：SQLite → 表前缀（`mgmt_`/`rt_`/`ins_`），MySQL → schema 限定名
   - 便捷函数 `t_mgmt(name)` / `t_runtime(name)` / `t_insight(name)`
   - 全量表注册表 `TABLE_REGISTRY`（85 张表 → 3 schema 的归属映射，design §4.1~4.3）
   - `ensure_schemas()`：MySQL `CREATE SCHEMA IF NOT EXISTS` / SQLite 无操作
   - 迁移辅助 `migrate_sqlite_prefixes()` / `migrate_mysql_schemas()`
2. `src/domains/contracts/` 空壳接口：`MgmtService` / `RuntimeService` / `InsightService`（ABC + 设计文档定义的方法签名，Phase 5 填充实现）
3. schema 迁移脚本 `scripts/migrate_schemas.py`：MySQL `CREATE SCHEMA` + `RENAME TABLE`；SQLite `ALTER TABLE ... RENAME TO`（保留数据）
4. SQLite 兼容层：表前缀映射（kernel/db.py 统一封装，业务代码无感）
5. SQL 表名替换：所有 SQL 中的表名替换为 `t_mgmt()` / `t_runtime()` / `t_insight()` 调用

## 2. 关键设计决策（How）

### 2.1 SQL 替换策略 — 运行时改写 + 渐进字面替换

实测：85 张表名在 `src/` 内有 **7828 处**字面引用、分布在 **152 个文件**。一次性字面替换（直接改写全部 SQL 字符串为 f-string + t_*()）在单次变更中不可安全验证。

采用**双通道**：

- **通道 A（本次交付，行为收口）**：`kernel/db.py.qualify_sql()` 运行时改写——在唯一写路径（`session_manager.db` SQLite 连接 / `mysql_compat.MySQLCompatDB`）的 execute 入口，将 SQL 中跟在 `FROM/JOIN/INTO/UPDATE/TABLE/TO` 等 SQL 关键字后的注册表表名自动映射为前缀/schema 限定名。业务代码零改动（与设计风险表"kernel/db.py 统一封装，业务代码无感"一致）。
  - 改写器**跳过单引号字符串字面量**（防数据值误改）；
  - 仅当表名出现在表名语法位置（关键字后）才改写（防列名/变量名误改，如列名 `agent_type` 与表同名）。
- **通道 B（工具交付，渐进采用）**：`scripts/replace_table_refs.py` 批量替换脚本，供 Phase 5 契约收口时把热点模块 SQL 逐文件迁移为显式 `t_*()` 调用。

### 2.2 灰度开关 `GRAPE_SCHEMA_MODE`

- 默认 **off**（未设置或 0/false）：`qualify_sql` 直通不改写，现有库与全量测试零回归。
- `GRAPE_SCHEMA_MODE=1`：启用前缀/schema 限定（对应 TC-2.3 单机模式 / TC-2.4 TC-2.5 集群模式行为）。
- `t_mgmt()/t_runtime()/t_insight()` 遵循 schema_mode_enabled()：模式关闭返回裸表名（存量库零回归），
  模式开启返回前缀/限定名。这样 codemod 通道 B 产物在两种模式下均可用，灰度降级语义不断裂。
  迁移函数用 `_qualified_table()` 强制取目标前缀名（不受开关影响）。

### 2.3 表注册表（TABLE_REGISTRY）

按 design §4.1~4.3 归属：mgmt 55 张（48 核心 + 附加/兼容）、runtime 22 张、insight 11 张；另含存量 `agent_type_config` → runtime。任何不在注册表中的表名（如历史表 `sessions`、`sqlite_master`）不做改写。

## 3. 验收（TC 映射）

| 用例 | 本期实现 |
|------|---------|
| TC-2.1 kernel/db.py 函数契约 | tests/test_kernel_db.py（sqlite 前缀 / mysql 限定双模式断言） |
| TC-2.2 MySQL schema 创建 | scripts/migrate_schemas.py --backend mysql（需真实 MySQL，交付脚本 + 单测模拟） |
| TC-2.3 SQLite 表前缀 | e2e 单测：临时库 + GRAPE_SCHEMA_MODE=1 建表后表名带 mgmt_/rt_/ins_ 前缀 |
| TC-2.4/2.5 跨 schema 查询/事务 | 运行时改写后同实例跨 schema 语义天然保留（同库前缀 / 同实例 schema 限定），MySQL 侧需真机验收（Phase 6 集群验证） |
| TC-2.6 contracts 空壳 | import 测试 |
| TC-2.7 全量替换 | 通道 A 达成行为等价；字面替换由通道 B 渐进收口（Phase 5），PR 说明 |

## 4. 非目标

- 不改任何业务表结构/数据（仅命名位置变化）。
- 不实现 contracts 的 Local/Remote provider（Phase 5）。
- 不做四进程拆分（Phase 4）。
