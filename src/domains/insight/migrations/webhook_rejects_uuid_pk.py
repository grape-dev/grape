"""grape_t_obs_webhook_rejects 键设计迁移（A6 表名前缀 + A12 字符串 UUID 主键）。

背景（PR #2157 架构守护）：
- A6：新增表一律 `grape_t_` 前缀；
- A12：新增表禁止自增主键（多实例/分库汇总按 id 关联会撞号）。

旧表 `obs_webhook_rejects`（自增 id）若已在库里落过数据，本迁移把行搬进新表并给每行
生成 uuid4().hex 主键，然后删除旧表（DROP 属危险 DDL，只走 `_destructive_ddl` 唯一入口，
过 min_code_version 门槛；被拦截则 raise 不盖章，下次启动重试）。

幂等：新表由 `schema_ddl` 启动期 CREATE IF NOT EXISTS 保证存在；旧表不存在即跳过；
行搬运用 INSERT OR IGNORE / INSERT IGNORE，重复执行不产生重复行。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

OLD_TABLE = "obs_webhook_rejects"

#: 搬运主键的固定命名空间（uuid5）：与旧行 id 一起决定新主键，使迁移可重试而幂等。
_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
NEW_TABLE = "grape_t_obs_webhook_rejects"
COLUMNS = ("repo_key", "provider", "event", "ip", "token_fp",
           "expected_fp", "reason", "created_at")


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
            # 存量库不跑老轨 _create_tables()（只在新装库首跑），新表必须由本迁移建，
            # 否则 DDL 里的新名字永远不落地（PR #2157 部署实测 1146）。
            from src.domains.insight.schema_ddl import _OBS_WEBHOOK_REJECTS_DDL
            cursor.execute(_OBS_WEBHOOK_REJECTS_DDL)
            if not _table_exists(cursor, OLD_TABLE):
                target.commit()
                return 0
            cols = ", ".join(COLUMNS)
            # SUP-1（PR #2245 复审）：主键必须由旧行**确定性派生**，否则 DROP 被清理门槛
            # 拦截后 raise 不盖章 → 下次启动重试会把同一批旧行再搬一遍，新表无其它 UNIQUE
            # 约束，INSERT IGNORE 永远不命中冲突 → 台账行翻倍。
            # uuid5(固定命名空间, "obs_webhook_rejects:<旧 id>") 让重试落到同一主键上，
            # INSERT IGNORE / INSERT OR IGNORE 因此真正幂等（双后端一致，不依赖 SQL 方言函数）。
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
                    "webhook rejects uuid migration: DROP blocked by cleanup gate "
                    "(min_code_version), not stamped")
            target.commit()
        finally:
            cursor.close()
    log.info(f"webhook rejects: moved {moved} row(s) to {NEW_TABLE} (UUID pk)")
    return moved


def converged(conn: Any = None) -> None:
    """post_check：旧表已不存在，新表存在（不收敛 raise，不盖章）。"""
    from src.api import db_setup as _ds

    target = _ds.db if conn is None else conn
    with _ds.db_lock:
        cursor = target.cursor()
        try:
            old_left = _table_exists(cursor, OLD_TABLE)
            new_ok = _table_exists(cursor, NEW_TABLE)
        finally:
            cursor.close()
    if old_left or not new_ok:
        raise RuntimeError(
            f"webhook rejects uuid migration not converged: old={old_left} new={new_ok}")