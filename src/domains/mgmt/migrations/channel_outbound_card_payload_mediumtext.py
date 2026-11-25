"""Issue #4339 同批审计：`grape_t_channel_outbound_card.payload` TEXT → MEDIUMTEXT。

为什么同批整改（而不是另立批次）
--------------------------------
该表是 IM 外发卡片的**唯一事实源**：入队时把整份卡片体 `json.dumps` 进 payload，
发送/重试都从这一列反序列化。列是 `TEXT(65,535)` 时，超长卡片（长 markdown 表格、
大段附件摘要）在 MySQL 非严格 `sql_mode` 下被**静默截断** → 反序列化失败或发出半截
卡片；而重试读的正是这一列，坏了就永远发不出正确内容。与 `events.payload`
（Issue #4339 主诉）是同一个病根，故同批收敛，口径与实现形态完全一致。

与 events 那条的差异只有表名、列定义（本表 `NOT NULL DEFAULT '{}'`）与版本号。

幂等与失败语义
--------------
- 类型探测短路：`DATA_TYPE` 已是 `mediumtext`/`longtext` → 零 DDL（`MODIFY COLUMN`
  是 COPY 算法锁写整表，不能每次启动重放）；
- `_destructive_ddl` 收口：MySQL 过 `meta.min_code_version` 门槛，返回 False（拦截）
  → 抛错不盖章；
- ALTER 后复检 `DATA_TYPE`：未变即抛错不盖章（`_destructive_ddl` 对非 MDL 异常 fail-open）；
- 非 owner（表不在本库 / 在本库是跨库只读视图）→ `_NonOwnerObject`，执行器跳过不盖章，
  由 mgmt owner 库执行；
- SQLite 无列长语义，分支直接返回（新装库口径由 `src/domains/mgmt/schema_ddl.py` 保证）。
"""
from __future__ import annotations

from contextlib import nullcontext

from src.kernel.config import log

TABLE = "grape_t_channel_outbound_card"
COLUMN = "payload"
#: 目标/更宽类型：已是二者之一即视为收敛。
TARGET_TYPES = ("mediumtext", "longtext")
MIGRATION_VERSION = "202611250002"
MIGRATION_NAME = "issue4339-channel-outbound-card-payload-mediumtext"

_PROBE_SQL = (
    "SELECT DATA_TYPE FROM information_schema.columns "
    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s")


def _db_setup():
    """延迟取 db_setup（避免启动期循环导入：db_setup 在文件末尾才 import 本模块）。"""
    from src.api import db_setup

    return db_setup


def _current_type(cursor) -> str:
    row = cursor.execute(_PROBE_SQL, (TABLE, COLUMN)).fetchone()
    if not row:
        raise RuntimeError(
            f"[Issue #4339] {TABLE}.{COLUMN} 列不存在（不在本库或探列失败）"
            "→ 迁移不盖章，下次启动重试")
    return str(row[0] or "").lower()


def migrate_channel_outbound_card_payload_mediumtext(conn=None) -> dict:
    """幂等收敛卡片表 payload 为 MEDIUMTEXT；返回本轮执行统计。"""
    ds = _db_setup()
    active = ds.db if conn is None else conn
    stats = {"backend": "sqlite" if not ds._mysql_backend() else "mysql",
             "before": "", "after": "", "altered": False}
    if stats["backend"] == "sqlite":
        log.info(f"[Issue #4339] SQLite: {TABLE}.{COLUMN} 无长度上限，跳过列类型收敛")
        return stats

    from src.domains.mgmt.migrations_ddl import (
        ALTER_GRAPE_T_CHANNEL_OUTBOUND_CARD__PAYLOAD_MEDIUMTEXT,
    )

    with (ds.db_lock if active is ds.db else nullcontext()):
        cursor = active.cursor()
        if not ds._table_present(cursor, TABLE):
            raise ds._NonOwnerObject(
                f"[Issue #4339] {TABLE} 不在本库（非 owner 域），"
                f"payload 列类型收敛由 mgmt 库执行")
        if not ds._is_base_table(cursor, TABLE):
            raise ds._NonOwnerObject(
                f"[Issue #4339] {TABLE} 在本库是视图（非 owner 域），"
                f"payload 列类型收敛由 mgmt 库执行")

        before = _current_type(cursor)
        stats["before"] = before
        if before in TARGET_TYPES:
            stats["after"] = before
            log.info(f"[Issue #4339] {TABLE}.{COLUMN} 已是 {before}，无需 ALTER")
            return stats

        if not ds._destructive_ddl(
                cursor, ALTER_GRAPE_T_CHANNEL_OUTBOUND_CARD__PAYLOAD_MEDIUMTEXT,
                kind_hint="cleanup", version=MIGRATION_VERSION):
            raise RuntimeError(
                f"[Issue #4339] {ALTER_GRAPE_T_CHANNEL_OUTBOUND_CARD__PAYLOAD_MEDIUMTEXT} "
                "被 cleanup 门槛拦截（meta.min_code_version 落后）→ 迁移不盖章，下版本重试")

        after = _current_type(cursor)
        stats["after"] = after
        if after not in TARGET_TYPES:
            raise RuntimeError(
                f"[Issue #4339] {TABLE}.{COLUMN} ALTER 后仍为 {after or '?'}"
                "（MODIFY COLUMN 未生效 / 无 ALTER 权限）→ 迁移不盖章，下次启动重试")
        active.commit()
        stats["altered"] = True
        log.info(f"[Issue #4339] {TABLE}.{COLUMN} {before} → {after}")
        return stats


def verify_channel_outbound_card_payload_mediumtext(conn=None) -> None:
    """post_check：只读探测列类型，非 mediumtext/longtext 即抛错（不盖章，下次重试）。"""
    ds = _db_setup()
    if not ds._mysql_backend():
        return
    active = ds.db if conn is None else conn
    got = _current_type(active.cursor())
    if got not in TARGET_TYPES:
        raise RuntimeError(
            f"[Issue #4339] post_check 未收敛：{TABLE}.{COLUMN} 仍为 {got or '?'}"
            "（不盖章，下次启动重试）")
    log.info(f"[Issue #4339] post_check 通过：{TABLE}.{COLUMN}={got}")