"""Column probes that work on both backends (Issue #2432 review).

`PRAGMA table_info(...)` is sqlite-only. The runtime database may sit behind
the thin compat cursor used in the three-database topology, which translates
parameters but does not rewrite PRAGMA statements, so a bare PRAGMA becomes a
1064 on MySQL and the probe silently returns False -- that would leave the new
`failed_node` column永远 unwritten on the deployments that matter.

Probe through information_schema on MySQL and PRAGMA on sqlite, mirroring the
approach already used in src/kernel/mysql_compat.py.
"""


def _is_mysql() -> bool:
    """当前进程后端是否 MySQL（探测失败按 sqlite 处理）。"""
    try:
        from src.kernel.session_manager import _is_mysql_backend
        return bool(_is_mysql_backend())
    except Exception:
        return False


def column_exists(cursor, table: str, column: str) -> bool:
    """该表是否已有指定列；探测失败一律返回 False（调用方按缺列降级）。"""
    if not table or not column:
        return False
    try:
        if _is_mysql():
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND COLUMN_NAME=? LIMIT 1",
                (table, column))
            return cursor.fetchone() is not None
        rows = cursor.execute(f"PRAGMA table_info({table})").fetchall()
        return column in [r[1] for r in rows]
    except Exception:
        return False


def relation_kind(cursor, table: str) -> str | None:
    """同名关系在**当前库**的形态：``"table"`` / ``"view"`` / ``None``（不存在）。

    Issue #3897：三库拓扑下非 owner 库只有跨域**视图**（owner 库才是基表）。
    `column_exists`/建表语句无法区分两者，启动自检与迁移需要这个判定：
      · ``None`` → 真缺（1146 根因，必须告警/补建）；
      · ``"view"`` → 跨域只读视图在位（合法形态，不要再建基表）。
    探测失败返回 ``None``（与 column_exists 同口径：宁可上报缺失，不静默放过）。
    """
    if not table:
        return None
    try:
        if _is_mysql():
            cursor.execute(
                "SELECT TABLE_TYPE FROM information_schema.tables "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? LIMIT 1", (table,))
            row = cursor.fetchone()
            if not row:
                return None
            _type = str(row[0] or "").upper()
            if _type == "VIEW":
                return "view"
            return "table" if _type == "BASE TABLE" else None
        row = cursor.execute(
            "SELECT type FROM sqlite_master WHERE name=? AND type IN ('table','view') "
            "LIMIT 1", (table,)).fetchone()
        if not row:
            return None
        return "view" if str(row[0]).lower() == "view" else "table"
    except Exception:
        return None