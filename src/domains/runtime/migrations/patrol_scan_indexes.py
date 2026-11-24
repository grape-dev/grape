"""Issue #3813：巡检候选扫描复合索引（runtime 域，迁移 202611231001）。

实测 EXPLAIN 基线（Issue #3813 根因）：巡检 dispatch 两形态候选查询在既有
单列索引下退化为大范围扫描——

  events（两形态）：
    patrol_a2a_unclaimed_rows      WHERE event_type=? AND status=? ... ORDER BY seq DESC
    patrol_a2a_failed_dispatch_rows WHERE event_type=? AND status=? AND (result LIKE ...×9)
  仅有 idx_events_status(status) 单列：failed 集上的 9 个前导通配 LIKE 逐行
  正扫 result（MEDIUMTEXT）——与候选数无关的固定成本主源（36-42s 实测）。

  agent_tasks（断链候选键集分页）：
    tasks_completed_page_cutoff   WHERE status=? AND completed_at>? AND id>? ORDER BY id LIMIT ?
  仅有 idx_agent_tasks_status(status)：completed 是表大头，每页都在 status 索引
  上回表过滤 completed_at。

索引列序依据（最左前缀：等值列在前、范围列居中、排序/键集游标列在尾）：
  idx_events_type_status_seq(event_type, status, seq)
    · event_type/status 双等值在最左——两形态候选查询与五路 S53 探针共用；
    · seq 排序列在尾——直接支撑 ORDER BY seq DESC（反向扫描），并让
      events_probe_* 纯索引命中（不回表）。
  idx_tasks_status_completed_id(status, completed_at, id)
    · status 等值最左；completed_at 范围窗居中；id 键集游标+排序在尾。
  MySQL 在线加索引（CREATE INDEX 为 INPLACE DDL，不锁 DML）；TEXT 列由
  mysql_compat 翻译器自动加 (191) 前缀长度。幂等：IF NOT EXISTS，重跑零写入。
"""
from __future__ import annotations

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_events_type_status_seq "
    "ON events(event_type, status, seq)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status_completed_id "
    "ON agent_tasks(status, completed_at, id)",
)


def ensure_patrol_scan_indexes() -> None:
    """在 owner 域（runtime）库建巡检候选复合索引；幂等（IF NOT EXISTS）。

    失败必须抛出（不吞）——执行器约定「抛错=不盖章，下次启动重试」；
    吞了会「索引没建成、版本号却盖死」（Issue #995 教训，register_migration
    自查五条之二）。
    """
    from src.kernel.config import log
    from src.kernel.session_manager import db, db_lock

    try:
        from src.kernel import table_ownership as _own
        for tbl in ("events", "agent_tasks"):
            if _own.should_skip(tbl):
                log.info(f"[DDL_SKIP] `{tbl}`: 归属 runtime，非本域跳过建索引")
                return
    except Exception as _e:  # noqa: BLE001 - 归属清单不可用时按本域表处理
        log.warning(f"table_ownership 不可用，按本域处理建索引: {_e}")

    with db_lock:
        c = db.cursor()
        for idx in INDEXES:
            c.execute(idx)
        db.commit()
    log.info("202611231001 巡检候选复合索引已在 owner 域（runtime）就位: "
             "[idx_events_type_status_seq, idx_tasks_status_completed_id]")


def verify_patrol_scan_indexes() -> None:
    """后置校验（Issue #3648 口径）：两条索引必须真的在位；缺则 raise 不盖章。

    SQLite 走 PRAGMA index_list / index_info；MySQL 由 mysql_compat 将 PRAGMA
    翻译为 information_schema（结果形状与 sqlite 对齐，db_setup 既有先例）。
    """
    from src.kernel.session_manager import db as conn

    cur = conn.cursor()
    for tbl, idx in (
        ("events", "idx_events_type_status_seq"),
        ("agent_tasks", "idx_tasks_status_completed_id"),
    ):
        rows = cur.execute(f"PRAGMA index_list({tbl})").fetchall() or []
        names = {r[1] for r in rows}
        if idx not in names:
            raise RuntimeError(
                f"索引 {idx} 未在 {tbl} 上建立（后置校验失败；不盖章，下次启动重试）")


MIGRATION = {
    "version": "202611231001",
    "name": "patrol-scan-composite-indexes",
    "kind": "safe",
    "fn": ensure_patrol_scan_indexes,
    "domains": ("runtime",),
}
