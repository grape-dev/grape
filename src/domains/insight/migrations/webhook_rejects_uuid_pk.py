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
from typing import Any

log = logging.getLogger(__name__)

OLD_TABLE = "obs_webhook_rejects"
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
            if _is_mysql_backend():
                cursor.execute(
                    f"INSERT IGNORE INTO {NEW_TABLE} (id, {cols}) "
                    f"SELECT REPLACE(UUID(),'-',''), {cols} FROM {OLD_TABLE}")
            else:
                cursor.execute(
                    f"INSERT OR IGNORE INTO {NEW_TABLE} (id, {cols}) "
                    f"SELECT lower(hex(randomblob(16))), {cols} FROM {OLD_TABLE}")
            moved = int(getattr(cursor, "rowcount", 0) or 0)
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