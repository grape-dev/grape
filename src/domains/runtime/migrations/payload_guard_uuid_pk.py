"""grape_t_autopilot_payload_guard 键设计迁移（A6 表名前缀 + A12 字符串 UUID 主键）。

背景（PR #2157 架构守护）：A6 新增表一律 `grape_t_` 前缀；A12 新增表禁止自增主键
（多实例/分库汇总按 id 关联会撞号）。

旧表 `autopilot_payload_guard`（自增 id）若已落过熔断台账，本迁移把行搬进新表并给每行
按旧行 id 确定性派生 uuid5(_ID_NAMESPACE, "autopilot_payload_guard:<旧行 id>") 主键（重试幂等），然后 DROP 旧表（危险 DDL 只走 `_destructive_ddl` 唯一入口，
过 min_code_version 门槛；被拦截 raise 不盖章）。

幂等：新表由 runtime `schema_ddl` 启动期 CREATE IF NOT EXISTS；旧表不存在即跳过；
搬运用 INSERT OR IGNORE / INSERT IGNORE（UNIQUE(payload_hash) 冲突行不重复写入）。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

OLD_TABLE = "autopilot_payload_guard"

#: 搬运主键的固定命名空间（uuid5）：与旧行 id 一起决定新主键，使迁移可重试而幂等。
_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
NEW_TABLE = "grape_t_autopilot_payload_guard"
COLUMNS = ("payload_hash", "task_id", "repo_key", "dispatch_count", "failure_count",
           "status", "reason", "first_seen_at", "last_seen_at", "updated_at")


def _table_exists(cursor: Any, name: str) -> bool:
    from src.kernel.session_manager import _is_mysql_backend

    if _is_mysql_backend():
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (name,))
    else:
        cursor.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,))
    row = cursor.fetchone()
    return bool(row and int(row[0] or 0))


def migrate(conn: Any = None) -> int:
    """搬运旧表行 → 新表（生成 UUID 主键）并删除旧表；返回搬运行数。"""
    from src.api import db_setup as _ds
    from src.kernel.session_manager import _is_mysql_backend

    target = _ds.db if conn is None else conn
    moved = 0
    with _ds.db_lock:
        cursor = target.cursor()
        try:
            # 存量库不跑老轨 _create_tables()，新表由本迁移建（见 insight 侧同款说明）。
            from src.domains.runtime.schema_ddl import _AUTOPILOT_PAYLOAD_GUARD_DDL
            cursor.execute(_AUTOPILOT_PAYLOAD_GUARD_DDL)
            if not _table_exists(cursor, OLD_TABLE):
                target.commit()
                return 0
            cols = ", ".join(COLUMNS)
            # 与 insight 侧同口径（PR #2245 复审 SUP-1）：主键由旧行确定性派生，使
            # 「DROP 被清理门槛拦截 → 下次启动重试」不会重复搬行。本表另有
            # UNIQUE(payload_hash) 兜底，两条幂等来源同时成立。
            is_mysql = _is_mysql_backend()
            ignore = "INSERT IGNORE" if is_mysql else "INSERT OR IGNORE"
            ph = "%s" if is_mysql else "?"
            insert_sql = (f"{ignore} INTO {NEW_TABLE} (id, {cols}) "
                          f"VALUES ({', '.join([ph] * (len(COLUMNS) + 1))})")
            rows = cursor.execute(f"SELECT id, {cols} FROM {OLD_TABLE}").fetchall()
            for row in rows:
                old_id, *rest = tuple(row)
                new_id = uuid.uuid5(_ID_NAMESPACE, f"{OLD_TABLE}:{old_id}").hex
                cursor.execute(insert_sql, (new_id, *rest))
                moved += int(getattr(cursor, "rowcount", 0) or 0)
            if not _ds._destructive_ddl(cursor, f"DROP TABLE {OLD_TABLE}",
                                        kind_hint="cleanup"):
                raise RuntimeError(
                    "payload guard uuid migration: DROP blocked by cleanup gate "
                    "(min_code_version), not stamped")
            target.commit()
        finally:
            cursor.close()
    log.info(f"autopilot payload guard: moved {moved} row(s) to {NEW_TABLE} (UUID pk)")
    return moved


def _id_primary_key_ok(cursor: Any) -> bool:
    """新表 `id` 必须是 VARCHAR(32) 字符串主键（A12）。

    `CREATE TABLE IF NOT EXISTS` 不会纠正「已存在但主键形态错误」的表，
    因此 post_check 必须自己复核主键类型，否则会盖章一个不合规的表。
    """
    from src.kernel.session_manager import _is_mysql_backend

    if _is_mysql_backend():
        cursor.execute(
            "SELECT COLUMN_TYPE, COLUMN_KEY FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME='id'",
            (NEW_TABLE,))
        row = cursor.fetchone()
        if not row:
            return False
        col_type, key = str(row[0] or '').lower(), str(row[1] or '').upper()
        return col_type.startswith('varchar(32)') and key == 'PRI'
    cursor.execute(f"PRAGMA table_info({NEW_TABLE})")
    for r in cursor.fetchall():
        if str(r[1]) == 'id':
            col_type = str(r[2] or '').upper().replace(' ', '')
            return col_type == 'VARCHAR(32)' and int(r[5] or 0) > 0
    return False


def converged(conn: Any = None) -> None:
    """post_check：旧表已不存在，新表存在（不收敛 raise，不盖章）。"""
    from src.api import db_setup as _ds

    target = _ds.db if conn is None else conn
    with _ds.db_lock:
        cursor = target.cursor()
        try:
            old_left = _table_exists(cursor, OLD_TABLE)
            new_ok = _table_exists(cursor, NEW_TABLE)
            pk_ok = _id_primary_key_ok(cursor) if new_ok else False
        finally:
            cursor.close()
    if old_left or not new_ok or not pk_ok:
        raise RuntimeError(
            f"payload guard uuid migration not converged: old={old_left} "
            f"new={new_ok} pk32={pk_ok}")