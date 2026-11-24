"""runtime_credentials 的 ACP state.db 治理指标列（迁移 202611240005）。

Issue #3773 让 worker 在 ``/api/runtime/heartbeat`` 上报 hermes ACP state.db 的
体积/清理/跳过指标，落 ``runtime_credentials`` 四列。心跳写侧这四列都在 UPDATE
列表里，**列不存在即 1054 Unknown column**，心跳整条语句失败被吞 → 心跳与
ACP 指标全部断掉（部署验证实测：``Unknown column 'acp_state_db_bytes'``）。

为什么单独一条注册式迁移（部署验证实测缺口）
--------------------------------------------
原实现把这 4 行 DDL 写进 `src/domains/runtime/migrations_ddl.py::ALTERS_ENGINE_CREDENTIAL_COLUMNS`
与 `src/api/db_setup.py::_migrate_tables()`（**冻结老轨**）：

- 存量库的 ``grape_t_schema_migrations`` 已被 legacy-stamp 盖帽（202610300001），
  启动时老轨整段不执行 → 存量库永远没有这 4 列；
- 全新库靠老轨补列、存量库靠注册式迁移补列，两条路径口径不一致，而
  `tests/api/test_migration_track_guard.py` 只比对 `_run_all_migrations()` 的调用
  清单、不校验 ``ALTERS_*`` 常量内容，门禁抓不到这类「新增列只登记在冻结老轨」。

因此这 4 列的口径统一到注册式迁移，``ALTERS_ENGINE_CREDENTIAL_COLUMNS`` 与该老轨
``_ensure_column`` 段落都不再保留它们。

归属：``runtime_credentials`` 归 runtime（configs/table_ownership.json），补列 SQL 住
本域 migrations 下（跨域守卫 P0-2）；注册由 ``src/api/db_setup.py`` 引导器做。

失败语义：补列失败要抛出（不盖章、下次启动重试）；``post_check`` 断言 4 列齐全才
盖章（Issue #3648）。

幂等：``_ensure_column`` 先探列，可重复执行。``domains=("runtime",)``：三库拓扑下由
runtime 域进程执行。

类型口径：读写两侧都是数值/JSON 串（`routes/runtime.py` 的 pydantic 模型是
float/str/float/int），故体积用 ``DOUBLE``（MySQL 里 ``REAL`` 在
``REAL_AS_FLOAT`` 下会退化成单精度，字节数会失精度），计数用 ``INT``，
清理明细是 JSON 串用 ``TEXT``（MySQL 侧默认值由 ``_ensure_column`` 剥掉后
INSTANT 补列，与 SQLite 的 ``DEFAULT ''`` 语义等价）。
"""
from __future__ import annotations

TABLE = "runtime_credentials"
#: (列名, 列定义) —— 与写侧 UPDATE/读侧 SELECT 的字段一一对应，逐字同 `routes/runtime.py`。
COLUMNS = (
    ("acp_state_db_bytes", "DOUBLE DEFAULT 0"),
    ("acp_state_db_cleanup_counts", "TEXT DEFAULT ''"),
    ("acp_state_db_bytes_freed_total", "DOUBLE DEFAULT 0"),
    ("acp_resume_skipped_total", "INT DEFAULT 0"),
)


def _columns_of(c, table: str) -> list:
    """当前表的列名（探列单一入口；MySQL 由 kernel mysql_compat 翻译）。"""
    from src.kernel.ddl_safety import table_columns

    return table_columns(c, table)


def add_acp_state_db_metrics_columns(conn=None) -> None:
    """幂等补 4 列（存量库首跑/重跑都安全）。"""
    from src.api import db_setup as ds
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with db_lock:
        c = conn.cursor()
        # 三库边界：表不在本库 / 在本库是只读视图 → 跳过不盖章，由 runtime 库收敛。
        if not ds._table_present(c, TABLE) or not ds._is_base_table(c, TABLE):
            raise ds._NonOwnerObject(
                f"{TABLE} 不在本库或是视图（非 owner 域），"
                f"acp_state_db_* 指标列由 runtime 库补齐")
        for col, ddl in COLUMNS:
            ds._ensure_column(c, TABLE, col, ddl, strict=True)
        conn.commit()


def verify_acp_state_db_metrics_columns(conn=None) -> None:
    """后置校验：4 列齐全才盖章；缺任意一列即抛错（下次启动重试）。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    present = set(_columns_of(conn.cursor(), TABLE))
    missing = [col for col, _ in COLUMNS if col not in present]
    if missing:
        raise RuntimeError(
            f"202611240005 post_check 未收敛：{TABLE} 缺列 {missing}"
            f"（不盖章，下次启动重试）")
    log.info(f"202611240005 post_check 通过：{TABLE} ACP 指标列齐全（{len(COLUMNS)} 列）")


MIGRATION = {
    "version": "202611240005",
    "name": "issue3773-acp-state-db-metrics-columns",
    "kind": "safe",
    "fn": add_acp_state_db_metrics_columns,
    "domains": ("runtime",),
    "post_check": verify_acp_state_db_metrics_columns,
}