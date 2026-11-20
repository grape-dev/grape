"""Column probes that work on both backends (Issue #2432 review).

`PRAGMA table_info(...)` is sqlite-only. The runtime database may sit behind
the thin compat cursor used in the three-database topology, which translates
parameters but does not rewrite PRAGMA statements, so a bare PRAGMA becomes a
1064 on MySQL and the probe silently returns False -- that would leave the new
`failed_node` column永远 unwritten on the deployments that matter.

Probe through information_schema on MySQL and PRAGMA on sqlite, mirroring the
approach already used in src/kernel/mysql_compat.py.
"""


def column_exists(cursor, table: str, column: str) -> bool:
    """该表是否已有指定列；探测失败一律返回 False（调用方按缺列降级）。"""
    if not table or not column:
        return False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        if _is_mysql_backend():
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND COLUMN_NAME=? LIMIT 1",
                (table, column))
            return cursor.fetchone() is not None
        rows = cursor.execute(f"PRAGMA table_info({table})").fetchall()
        return column in [r[1] for r in rows]
    except Exception:
        return False
