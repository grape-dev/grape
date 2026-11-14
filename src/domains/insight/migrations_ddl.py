"""迁移 SQL：insight 域归属表的 DDL/DML（由 db_setup 拆分而来）。

为什么在域模块里：`src/api/db_setup.py` 是 schema 引导器，却曾放着三域全部迁移 SQL；
静态扫描（scripts/foreign_table_refs.py）按文件路径判消费域，把这些 SQL 全算成
「api 消费外域表」。SQL 属表的归属域（方案 D9「每域自己的 schema」），放 owner 域
模块后扫描结果才反映真实依赖。

搬运工具：`scripts/split_migration_sql.py`（AST 定位字面量，原文搬迁）。
行为不变：`db_setup` 仍按原顺序执行同一批 SQL，只是字符串从本模块 import。
本模块只放**字符串常量**，不含执行逻辑——顺序由 db_setup 的编排保证。
"""

# ── 自 db_setup.backfill_gitee_gitlab_token_columns_to_rc() 下沉 ─────
INSERT_IGNORE_GRAPE_T_REPO_CREDENTIALS = (
    "INSERT OR IGNORE INTO grape_t_repo_credentials (id, repo_key, cred_type, value) "
    "VALUES (?, ?, ?, ?)"
)
