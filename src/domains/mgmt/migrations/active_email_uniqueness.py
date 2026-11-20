"""grape_accounts 活跃邮箱唯一约束（Issue #3469 O）——迁移对象。

**为什么单独成模块**：`db_setup.py` 已是巨型文件，迁移 SQL/双后端方言/复查逻辑
不该再往里堆。这里按「聚合对象 + 迁移执行器」建模：

- `ActiveEmailUniquenessIndex`：唯一约束本身的领域实现（SQLite 部分索引 /
  MySQL STORED 生成列），只认一个游标与后端类型，可单测、可复查；
- `ActiveEmailUniquenessMigration`：迁移执行器（owner 域守卫 → 建索引 → 提交），
  由 `db_setup.register_migration()` 注册；owner 判定借用 db_setup 的
  `_table_present / _is_base_table / _NonOwnerObject`（调用期导入，避免循环依赖）。

口径（docs/weekly-11/email-verification-design.md D10）：
`active_email = IF(status IN (0,1,3), email, NULL)` 上的唯一索引；`''` 归一 NULL。
`status=1`（活跃）与 `3`（冷静期）都占邮箱，`0/2/4` 不再占键。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

# 业务侧 `is_email_registered()` 视为「持有邮箱」的状态集合（单一来源）
# Issue #3469 复审 P1：0=管理员禁用也在内。原口径只认 (1,3)，被禁用账号的 email
# 不占键 → 别人可拿同一邮箱注册成功，读侧 `WHERE email=?` 命中两行时取谁不保证
# （实测 MySQL：先插 status=0，再插 status=1 同 email 两行并存，LIMIT 1 取到禁用那行）。
# 保留禁用账号的邮箱即消除这种歧义；2（已注销）/4（已合并）仍是释放态（email 置空）。
PLACEHOLDER_EMAIL_STATUSES: tuple[int, ...] = (0, 1, 3)
ACTIVE_EMAIL_STATUSES: tuple[int, ...] = PLACEHOLDER_EMAIL_STATUSES

_SQLITE_CREATE = (
    "CREATE UNIQUE INDEX uq_active_email ON grape_accounts(email) "
    f"WHERE status IN ({', '.join(str(s) for s in ACTIVE_EMAIL_STATUSES)}) "
    "AND email IS NOT NULL"
)
_MYSQL_ADD_COLUMN = (
    "ALTER TABLE grape_accounts ADD COLUMN active_email VARCHAR(255) "
    f"AS (CASE WHEN status IN ({', '.join(str(s) for s in ACTIVE_EMAIL_STATUSES)}) "
    "THEN email ELSE NULL END) STORED"
)
_MYSQL_CREATE = "CREATE UNIQUE INDEX uq_active_email ON grape_accounts (active_email)"


def _statuses_in(text: str) -> tuple[int, ...]:
    """从「status IN (…, …)」里取状态集合（忽略反引号/多余括号/空白）。"""
    import re as _re

    m = _re.search(r"status\W*in\s*\(([0-9,\s]+)\)", str(text or ""), _re.IGNORECASE)
    if not m:
        return ()
    try:
        return tuple(sorted({int(x) for x in m.group(1).replace(" ", "").split(",") if x}))
    except ValueError:
        return ()


class ActiveEmailUniquenessIndex:
    """「仅活跃状态唯一」索引（幂等）。"""

    def __init__(self, cursor: Any, is_mysql: bool,
                 drop_index: Callable[[str], bool] | None = None) -> None:
        self._c = cursor
        self._is_mysql = is_mysql
        self._drop_index = drop_index or self._default_drop

    # -- 方言探针 ---------------------------------------------------------
    def columns(self) -> list[str]:
        """uq_active_email 当前索引列（不存在 → []）。"""
        if self._is_mysql:
            rows = self._c.execute(
                "SELECT COLUMN_NAME FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='grape_accounts' "
                "AND INDEX_NAME='uq_active_email' ORDER BY SEQ_IN_INDEX").fetchall()
            return [str(r[0]) for r in (rows or [])]
        rows = self._c.execute("PRAGMA index_info(uq_active_email)").fetchall() or []
        return [str(r[2]) for r in rows]

    def sqlite_sql(self) -> str:
        row = self._c.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_active_email'"
        ).fetchone()
        return str(row[0] or "") if row else ""

    def duplicate_groups(self) -> int:
        """占键账号里 email 重复的组数（建索引失败时进报错，便于人工清理）。"""
        statuses = ", ".join(str(x) for x in ACTIVE_EMAIL_STATUSES)
        try:
            row = self._c.execute(
                "SELECT COUNT(*) FROM (SELECT email FROM grape_accounts "
                f"WHERE status IN ({statuses}) AND email IS NOT NULL "
                "GROUP BY email HAVING COUNT(*) > 1) d").fetchone()
            return int(row[0] or 0) if row else 0
        except Exception:  # noqa: BLE001 - 诊断信息失败不影响主流程
            return -1

    def _default_drop(self, ddl: str) -> bool:
        from src.api.db_setup import _destructive_ddl

        return bool(_destructive_ddl(self._c, ddl, kind_hint="cleanup"))

    # -- 主流程 -----------------------------------------------------------
    def ensure(self) -> dict:
        stats: dict = {"engine": "mysql" if self._is_mysql else "sqlite",
                       "blank_to_null": 0, "dropped_old": False,
                       "created": False, "already_ok": False}
        self._normalize_blank_email(stats)
        if self._is_mysql:
            self._ensure_mysql(stats)
        else:
            self._ensure_sqlite(stats)
        if stats["created"] or stats["dropped_old"] or stats["blank_to_null"]:
            log.info(f"[Issue #3469] uq_active_email 迁移完成: {stats}")
        return stats

    def _normalize_blank_email(self, stats: dict) -> None:
        """'' → NULL：空串在唯一键上占键，业务侧却当"无邮箱"（D10 的另一半）。"""
        row = self._c.execute(
            "SELECT COUNT(*) FROM grape_accounts WHERE email = ''").fetchone()
        blank = int(row[0] or 0) if row else 0
        if blank:
            self._c.execute("UPDATE grape_accounts SET email = NULL WHERE email = ''")
            stats["blank_to_null"] = blank

    def _ensure_mysql(self, stats: dict) -> None:
        row = self._c.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='grape_accounts' "
            "AND COLUMN_NAME='active_email'").fetchone()
        if not (row and int(row[0] or 0)):
            self._c.execute(_MYSQL_ADD_COLUMN)
            log.info("[Issue #3469] grape_accounts 增加 STORED 生成列 active_email")

        expr_row = self._c.execute(
            "SELECT GENERATION_EXPRESSION FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='grape_accounts' "
            "AND COLUMN_NAME='active_email'").fetchone()
        expr = str(expr_row[0] or "") if expr_row else ""
        if expr and _statuses_in(expr) != tuple(sorted(ACTIVE_EMAIL_STATUSES)):
            # 口径升级（如 (1,3) → (0,1,3)）：旧索引必须拆掉重建，否则被禁用账号的
            # 邮箱仍不占键（复审 P1 的真实故障面）。
            if self.columns() and not self._drop_index("DROP INDEX uq_active_email ON grape_accounts"):
                raise RuntimeError(
                    "[Issue #3469] DROP INDEX uq_active_email 被 cleanup 门槛拦截（口径升级），"
                    "迁移不盖章（下版本重试）")
            self._c.execute("ALTER TABLE grape_accounts DROP COLUMN active_email")
            self._c.execute(_MYSQL_ADD_COLUMN)   # 口径升级后必须按新表达式重建
            log.info("[Issue #3469] active_email 生成列口径升级 → 重建 (0,1,3)")

        old_cols = self.columns()
        if old_cols and old_cols != ["active_email"]:
            if not self._drop_index("DROP INDEX uq_active_email ON grape_accounts"):
                raise RuntimeError(
                    "[Issue #3469] DROP INDEX uq_active_email 被 cleanup 门槛拦截，"
                    "迁移不盖章（下版本重试）")
            stats["dropped_old"] = True
            log.info(f"[Issue #3469] 已删除旧的全列索引 uq_active_email {old_cols}")

        if not self.columns():
            try:
                self._c.execute(_MYSQL_CREATE)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    "[Issue #3469] 创建 uq_active_email(active_email) 失败："
                    f"{e}（活跃账号 email 重复组数={self.duplicate_groups()}；"
                    ">0 需先人工清理）") from e
            stats["created"] = True

        cols = self.columns()
        if cols != ["active_email"]:
            raise RuntimeError(
                f"[Issue #3469] uq_active_email 复查失败：当前列 {cols}，期望 ['active_email']")
        extra_row = self._c.execute(
            "SELECT EXTRA FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='grape_accounts' "
            "AND COLUMN_NAME='active_email'").fetchone()
        extra = str(extra_row[0] or "").upper() if extra_row else ""
        if "STORED GENERATED" not in extra:
            raise RuntimeError(
                f"[Issue #3469] active_email 生成列复查失败：EXTRA={extra!r}，"
                "期望含 'STORED GENERATED'")

    def _ensure_sqlite(self, stats: dict) -> None:
        old_sql = self.sqlite_sql()
        if old_sql:
            if _statuses_in(old_sql) != tuple(sorted(ACTIVE_EMAIL_STATUSES)):
                if not self._drop_index("DROP INDEX uq_active_email"):
                    raise RuntimeError(
                        "[Issue #3469] DROP INDEX uq_active_email 被 cleanup 门槛拦截（口径升级），"
                        "迁移不盖章（下版本重试）")
                stats["dropped_old"] = True
                log.info(f"[Issue #3469] 旧部分索引状态集合不符 → 重建 {ACTIVE_EMAIL_STATUSES}")
                old_sql = ""
        if old_sql and " WHERE " not in f" {old_sql} ".upper():
            if not self._drop_index("DROP INDEX uq_active_email"):
                raise RuntimeError(
                    "[Issue #3469] DROP INDEX uq_active_email 被 cleanup 门槛拦截，"
                    "迁移不盖章（下版本重试）")
            stats["dropped_old"] = True
            log.info("[Issue #3469] 已删除旧的全列索引 uq_active_email（无 WHERE）")
            old_sql = ""

        if not old_sql:
            try:
                self._c.execute(_SQLITE_CREATE)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    "[Issue #3469] 创建部分唯一索引 uq_active_email 失败："
                    f"{e}（活跃账号 email 重复组数={self.duplicate_groups()}；"
                    ">0 需先人工清理）") from e
            stats["created"] = True

        sql_now = self.sqlite_sql()
        up = f" {sql_now} ".upper()
        if not sql_now or " WHERE " not in up or "STATUS IN" not in up:
            raise RuntimeError(
                f"[Issue #3469] uq_active_email 复查失败：定义 {sql_now!r} 不是"
                "「status IN (0,1,3) 的部分唯一索引」")
        if not stats["created"] and not stats["dropped_old"]:
            stats["already_ok"] = True


class ActiveEmailUniquenessMigration:
    """迁移执行器：owner 域守卫 + 建索引 + 提交（不盖章由抛错实现）。"""

    NAME = "issue3469-active-email-partial-unique"

    def __call__(self, conn: Any = None) -> dict:
        from src.api import db_setup as _ds

        target = _ds.db if conn is None else conn
        cursor = target.cursor()
        if (not _ds._table_present(cursor, "grape_accounts")
                or not _ds._is_base_table(cursor, "grape_accounts")):
            # 非 owner 库（缺表 / 跨库视图）：抛出让执行器跳过且不盖章
            raise _ds._NonOwnerObject(
                "grape_accounts 不在本库或是视图（非 owner 域），"
                "活跃 email 唯一索引由 mgmt 库建")
        stats = ActiveEmailUniquenessIndex(cursor, _ds._mysql_backend()).ensure()
        target.commit()
        return stats


def migrate_active_email_uniqueness(conn: Any = None) -> dict:
    """注册给 `register_migration(fn=...)` 的薄函数（checksum 可取证）。"""
    return ActiveEmailUniquenessMigration()(conn)


def ensure_active_email_index(cursor: Any, is_mysql: bool | None = None) -> dict:
    """新装库建表路径复用同一份实现（与升级库终态一致）。"""
    if is_mysql is None:
        from src.api.db_setup import _mysql_backend

        is_mysql = _mysql_backend()
    return ActiveEmailUniquenessIndex(cursor, is_mysql).ensure()