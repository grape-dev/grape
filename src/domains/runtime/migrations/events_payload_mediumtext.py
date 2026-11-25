"""Issue #4339：events.payload 由 TEXT(65,535) 升 MEDIUMTEXT(16,777,215)。

背景
----
`events` 表里 `agent_logs` / `result` / `structured_output` 早已是 MEDIUMTEXT，只有
`payload` 漏在 TEXT 上（`src/domains/runtime/schema_ddl.py::_EVENTS_TABLE_DDL` 原
`payload TEXT DEFAULT '{}'`）。MySQL **非严格** `sql_mode` 下，超 64KB 的事件 payload
写入不是报错而是**静默截断**：生产 runtime 库 seq 107279（issue_opened）与 107291
（issue_updated）两行都恰好 `LENGTH=65535` 且 `JSON_VALID=0` —— 半截 JSON 不可解析，
`src/kernel/event_payload_view.py::payload_view_from_json` 解析失败后整份 payload 视图
降级为空，下游 prompt 组装 / 架构守卫影响面 / 前端事件详情全部丢上下文（静默数据丢失）。

本迁移只做「存量库列类型收敛」，不迁数据、不改历史坏行（原文已丢，无法恢复）。

为什么必须探测短路
------------------
`MODIFY COLUMN` 在 MySQL 上是 COPY 算法：重建整张表并阻塞写入。若每次启动无条件执行，
`events` 这张热表的每次进程重启都要重建一遍并拿元数据锁（历史上启动变慢的根因形态）。
因此先查 `information_schema.columns.DATA_TYPE`：已 `mediumtext`/`longtext` 直接返回，
**重复启动零 DDL**。

为什么要 `_destructive_ddl` + 复检
--------------------------------
`src/api/db_setup.py::_destructive_ddl` 是危险 DDL 的唯一收口点：MySQL 侧要过
`meta.min_code_version` 门槛（蓝绿共存期落后实例跳过），并保证真失败**抛出**（执行器
以「fn 正常返回 = 盖章」为约定）。但 `_destructive_ddl` 对非 MDL 异常是 fail-open 的
（只记 WARNING 仍返回 True），所以「ALTER 成功」必须自己复检 `DATA_TYPE`：未变即抛错、
不盖章、下次启动重试，否则列永远停在 TEXT。

为什么常量必须带 `MODIFY COLUMN`
--------------------------------
迁移执行器按「语句是否含 `MODIFY COLUMN`」把 ALTER 路由进 `_destructive_ddl`；历史先例
`migrations_ddl.ALTER_AGENT_TASKS = "ALTER TABLE agent_tasks MODIFY skill_files MEDIUMTEXT"`
缺 `COLUMN`，会走裸 `db.execute` 绕过门槛、失败也被外层 `except` 吞掉。本条常量
`ALTER_EVENTS__EVENTS_PAYLOAD_MEDIUMTEXT` 显式带 `COLUMN`（有用例锁定）。

三库边界
--------
`events` 归 runtime（`configs/table_ownership.json`）。非 owner 域库看到的是跨库只读
**视图**（`TABLE_TYPE != BASE TABLE`），对视图 ALTER 会 1346/1347 → 抛 `_NonOwnerObject`，
执行器跳过且**不盖章**，由 runtime owner 库执行同一条登记迁移。

SQLite
------
SQLite 的列类型不参与长度校验（无 65,535 上限），新装库口径由 `schema_ddl` 的
`MEDIUMTEXT` 文本保证；存量 SQLite 库保持原声明即可，本迁移在 SQLite 分支直接返回、
不执行任何 DDL。
"""
from __future__ import annotations

from contextlib import nullcontext

from src.kernel.config import log

TABLE = "events"
COLUMN = "payload"
#: 目标/更宽类型：已是二者之一即视为收敛（longtext 比 mediumtext 更宽，无需收窄）。
TARGET_TYPES = ("mediumtext", "longtext")
#: 注册迁移版本号（与 src/api/db_setup.py 的 register_migration 字面量一致）。
#: 原为 202611250001，与同批三条迁移撞号（PR #2157 门禁复审 Critical：台账按
#: version 判「已执行」，同号后登记者永久跳过）→ renumber 到 202611250003；
#: 001 保留给 events-cascade-compensation-columns。
MIGRATION_VERSION = "202611250003"
MIGRATION_NAME = "issue4339-events-payload-mediumtext"

_PROBE_SQL = (
    "SELECT DATA_TYPE FROM information_schema.columns "
    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s")


def _db_setup():
    """延迟取 db_setup（避免启动期循环导入：db_setup 在文件末尾才 import 本模块）。"""
    from src.api import db_setup

    return db_setup


def _current_type(cursor) -> str:
    """当前库里 `events.payload` 的 DATA_TYPE（缺失即抛错：owner 表缺列不是可跳过状态）。"""
    row = cursor.execute(_PROBE_SQL, (TABLE, COLUMN)).fetchone()
    if not row:
        raise RuntimeError(
            f"[Issue #4339] {TABLE}.{COLUMN} 列不存在（不在本库或探列失败）"
            "→ 迁移不盖章，下次启动重试")
    return str(row[0] or "").lower()


def migrate_events_payload_mediumtext(conn=None) -> dict:
    """幂等收敛 `events.payload` 为 MEDIUMTEXT；返回本轮执行统计（日志/用例断言用）。"""
    ds = _db_setup()
    active = ds.db if conn is None else conn
    stats = {"backend": "sqlite" if not ds._mysql_backend() else "mysql",
             "before": "", "after": "", "altered": False}
    if stats["backend"] == "sqlite":
        # SQLite 无列长语义：口径由 schema_ddl 保证，存量库无需 DDL。
        log.info(f"[Issue #4339] SQLite: {TABLE}.{COLUMN} 无长度上限，跳过列类型收敛")
        return stats

    from src.domains.runtime.migrations_ddl import (
        ALTER_EVENTS__EVENTS_PAYLOAD_MEDIUMTEXT,
    )

    with (ds.db_lock if active is ds.db else nullcontext()):
        cursor = active.cursor()
        if not ds._table_present(cursor, TABLE):
            raise ds._NonOwnerObject(
                f"[Issue #4339] {TABLE} 不在本库（非 owner 域），"
                f"payload 列类型收敛由 runtime 库执行")
        if not ds._is_base_table(cursor, TABLE):
            raise ds._NonOwnerObject(
                f"[Issue #4339] {TABLE} 在本库是视图（非 owner 域），"
                f"payload 列类型收敛由 runtime 库执行")

        before = _current_type(cursor)
        stats["before"] = before
        if before in TARGET_TYPES:
            stats["after"] = before
            log.info(f"[Issue #4339] {TABLE}.{COLUMN} 已是 {before}，无需 ALTER")
            return stats

        if not ds._destructive_ddl(
                cursor, ALTER_EVENTS__EVENTS_PAYLOAD_MEDIUMTEXT,
                kind_hint="cleanup", version=MIGRATION_VERSION):
            raise RuntimeError(
                f"[Issue #4339] {ALTER_EVENTS__EVENTS_PAYLOAD_MEDIUMTEXT} "
                "被 cleanup 门槛拦截（meta.min_code_version 落后）→ 迁移不盖章，下版本重试")

        # 复检：DDL 没报错不等于生效（_destructive_ddl 对非 MDL 异常 fail-open）。
        after = _current_type(cursor)
        stats["after"] = after
        if after not in TARGET_TYPES:
            raise RuntimeError(
                f"[Issue #4339] {TABLE}.{COLUMN} ALTER 后仍为 {after or '?'}"
                "（MODIFY COLUMN 未生效 / 无 ALTER 权限）→ 迁移不盖章，下次启动重试")
        active.commit()
        stats["altered"] = True
        log.info(f"[Issue #4339] {TABLE}.{COLUMN} {before} → {after}（COPY 算法，首次升级锁写整表）")
        return stats


def verify_events_payload_mediumtext(conn=None) -> None:
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