"""Issue #3813：巡检 findings 表查询复合索引（mgmt 域，迁移 202611231002）。

表归属：grape_t_patrol_findings → mgmt（configs/table_ownership.json）。
查询形状（src/domains/mgmt/public/__init__.py 巡检原语）：
  patrol_findings_known          SELECT kind, task_id ... WHERE task_id != ''
  patrol_findings_delete_terminal DELETE ... WHERE status IN (...) AND updated_at < ?

索引列序依据（最左前缀）：
  idx_patrol_findings_task_kind(task_id, kind)
    · known-set 拉取的过滤列 task_id 在最左、投影列 kind 在尾——纯索引覆盖
      （covering scan，不回表），audit 重扫短路集的 O(findings) 拉取成本减半。
  idx_patrol_findings_status_updated(status, updated_at)
    · TTL 清理 DELETE 的 status IN（等值）在前、updated_at < cutoff（范围）在尾
      ——终态行清理从全表回表过滤收敛为索引定位删除。
幂等：IF NOT EXISTS，重跑零写入；MySQL TEXT 列由兼容层自动加 (191) 前缀。
"""
from __future__ import annotations

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_patrol_findings_task_kind "
    "ON grape_t_patrol_findings(task_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_patrol_findings_status_updated "
    "ON grape_t_patrol_findings(status, updated_at)",
)


def ensure_patrol_findings_indexes() -> None:
    """在 owner 域（mgmt）库建 findings 查询索引；幂等。失败抛出（不吞）。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db, db_lock

    try:
        from src.kernel import table_ownership as _own
        if _own.should_skip("grape_t_patrol_findings"):
            log.info("[DDL_SKIP] `grape_t_patrol_findings`: 归属 mgmt，非本域跳过建索引")
            return
    except Exception as _e:  # noqa: BLE001 - 归属清单不可用时按本域表处理
        log.warning(f"table_ownership 不可用，按本域处理建索引: {_e}")

    with db_lock:
        c = db.cursor()
        for idx in INDEXES:
            c.execute(idx)
        db.commit()
    log.info("202611231002 巡检 findings 查询索引已在 owner 域（mgmt）就位: "
             "[idx_patrol_findings_task_kind, idx_patrol_findings_status_updated]")


def verify_patrol_findings_indexes() -> None:
    """后置校验：两条索引在位；缺则 raise 不盖章（下次启动重试）。"""
    from src.kernel.session_manager import db as conn

    cur = conn.cursor()
    rows = cur.execute("PRAGMA index_list(grape_t_patrol_findings)").fetchall() or []
    names = {r[1] for r in rows}
    for idx in ("idx_patrol_findings_task_kind", "idx_patrol_findings_status_updated"):
        if idx not in names:
            raise RuntimeError(
                f"索引 {idx} 未在 grape_t_patrol_findings 上建立"
                f"（后置校验失败；不盖章，下次启动重试）")


MIGRATION = {
    "version": "202611231002",
    "name": "patrol-findings-scan-indexes",
    "kind": "safe",
    "fn": ensure_patrol_findings_indexes,
    "domains": ("mgmt",),
}
