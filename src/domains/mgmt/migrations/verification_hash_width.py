"""verification_codes.code_hash 列宽收敛（Issue #3469 I 复审修复）。

背景：验证码哈希自 Issue #3469 起存 ``"{kid}:{digest}"``（kid 来自 JWT 签名密钥池，
用于密钥轮换期兼容旧码）。DDL 原来只有 ``VARCHAR(64)``——dust 长度 64 之外再带
``kid:`` 前缀就装不下：

- MySQL 严格模式：INSERT 直接 1406（Data too long）→ `send_code` 100% 失败；
- MySQL 非严格模式：静默截断 → 所有码都校验不过；
- SQLite 不校验长度 → 单测全绿也发现不了。

本迁移把列加宽到 ``VARCHAR(191)``（与仓内其他 TEXT/VARCHAR 宽度口径一致，
SQLite 侧无长度语义、无需改表）。迁移对象与 `db_setup.py` 的注册分离，
巨型文件只留一行接线。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

TARGET_WIDTH = 191
TABLE = "verification_codes"
COLUMN = "code_hash"


class VerificationHashWidthMigration:
    """把 ``verification_codes.code_hash`` 加宽到 191（幂等）。"""

    NAME = "issue3469-verification-code-hash-width"

    def __call__(self, conn: Any = None) -> dict:
        from src.api import db_setup as _ds

        target = _ds.db if conn is None else conn
        cursor = target.cursor()
        if (not _ds._table_present(cursor, TABLE)
                or not _ds._is_base_table(cursor, TABLE)):
            raise _ds._NonOwnerObject(
                "verification_codes 不在本库或是视图（非 owner 域），"
                "code_hash 列宽由 mgmt 库收敛")

        stats = {"engine": "mysql" if _ds._mysql_backend() else "sqlite",
                 "before": None, "after": None, "altered": False}

        if not _ds._mysql_backend():
            # SQLite 的 VARCHAR 不参与长度约束，新装库口径由 schema_ddl 保证
            stats["after"] = TARGET_WIDTH
            target.commit()
            return stats

        row = cursor.execute(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            (TABLE, COLUMN)).fetchone()
        before = int(row[0] or 0) if row else 0
        stats["before"] = before
        if before and before >= TARGET_WIDTH:
            stats["after"] = before
            target.commit()
            return stats

        ddl = f"ALTER TABLE {TABLE} MODIFY {COLUMN} VARCHAR({TARGET_WIDTH}) NOT NULL"
        if not _ds._destructive_ddl(cursor, ddl, kind_hint="cleanup"):
            raise RuntimeError(
                f"[Issue #3469] {ddl} 被 cleanup 门槛拦截，迁移不盖章（下版本重试）")

        # 复查：以最终 schema 为准，DDL 没报错不等于生效
        row = cursor.execute(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            (TABLE, COLUMN)).fetchone()
        after = int(row[0] or 0) if row else 0
        stats["after"] = after
        if after < TARGET_WIDTH:
            raise RuntimeError(
                f"[Issue #3469] code_hash 列宽复查失败：当前 {after}，期望 >= {TARGET_WIDTH}")
        stats["altered"] = True
        target.commit()
        log.info(f"[Issue #3469] verification_codes.code_hash 加宽 {before} -> {after}")
        return stats


def migrate_verification_hash_width(conn: Any = None) -> dict:
    """注册给 `register_migration(fn=...)` 的薄函数（checksum 可取证）。"""
    return VerificationHashWidthMigration()(conn)