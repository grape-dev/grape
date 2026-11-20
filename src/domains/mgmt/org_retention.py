"""组织下派/快照保留策略（A7 整改：大表分区/归档的等价实现）。

设计口径（docs/org-pipeline/org-runtime-budget.md §4）：
- 下派批次/目标/聚合台账是事件类大表，组织存活期内会随每次需求下派增长；
  终态批次超过保留期的行按「聚合 → 目标 → 快照 → 批次」顺序清理；
- 组织上下文快照正文在磁盘 `WORKSPACE_DIR/context-snapshots/` 下，DB 只存
  manifest；清理 DB 行时同步删除对应目录，避免磁盘无界增长；
- 非终态批次永不清理（仍在推进/可人工取消），批次 id 被 issue 引用时保留；
- 全部操作幂等，可被周期任务/运维脚本重复执行。
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone

from src.kernel.config import WORKSPACE_DIR, log
from src.kernel.session_manager import db, db_lock

# rationale（数值登记 docs/org-pipeline/org-runtime-budget.md §4）：保留 90 天与
# patrol findings 默认保留期一致，覆盖「月度复盘 + 季度审计」窗口；组织删除时的
# 即时清理仍由 org_provision.delete_org_with_cleanup 承担，本策略只处理存活组织。
_RETENTION_DAYS = int(os.environ.get("ORG_DISPATCH_RETENTION_DAYS", "90") or 90)  # rationale: docs/org-pipeline/org-runtime-budget.md §4
# rationale：清理扫描不需要高频——每天一次即可；与 60s sweeper 解耦，避免每小时
# 全表扫描。0 表示关闭周期清理（仍可手工调用 purge_expired_org_records）。
_RETENTION_INTERVAL_SECONDS = int(
    os.environ.get("ORG_RETENTION_INTERVAL_SECONDS", "86400") or 86400)  # rationale: 同 doc §4（每日一次）

_TERMINAL_STATUSES = ("completed", "needs_review", "partial_failed", "failed", "cancelled")
_last_purge_at = 0.0


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))).strftime("%Y-%m-%d %H:%M:%S")


def _remove_snapshot_dirs(paths: list[str]) -> int:
    removed = 0
    base = os.path.abspath(WORKSPACE_DIR)
    for rel in paths:
        rel = str(rel or "").strip()
        if not rel:
            continue
        target = os.path.abspath(os.path.join(base, rel))
        if not target.startswith(base + os.sep):
            log.warning(f"org retention: skip out-of-workspace snapshot path {rel!r}")
            continue
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
            removed += 1
    return removed


def purge_expired_org_records(retention_days: int = -1) -> dict:
    """清理超过保留期的终态下派批次、目标/聚合行与快照（含磁盘目录）。

    ``retention_days < 0`` 读 env（ORG_DISPATCH_RETENTION_DAYS，默认 90）。
    返回清理统计；幂等。
    """
    days = _RETENTION_DAYS if retention_days < 0 else int(retention_days)
    if days <= 0:
        return {"skipped": True, "reason": "retention_disabled"}
    cutoff = _cutoff(days)
    placeholders = ",".join("?" * len(_TERMINAL_STATUSES))
    with db_lock:
        c = db.cursor()
        dispatch_ids = [
            str(r[0] or "") for r in c.execute(
                f"SELECT id FROM grape_t_org_dispatch WHERE status IN ({placeholders}) "
                "AND COALESCE(created_at, '') < ?",
                tuple(_TERMINAL_STATUSES) + (cutoff,),
            ).fetchall()
        ]
        snapshots = []
        aggregates = targets = snap_rows = dispatch_rows = 0
        if dispatch_ids:
            marks = ",".join("?" * len(dispatch_ids))
            snapshots = c.execute(
                f"SELECT storage_path FROM grape_t_org_context_snapshot "
                f"WHERE dispatch_id IN ({marks})", tuple(dispatch_ids)).fetchall()
            aggregates = c.execute(
                f"DELETE FROM grape_t_org_dispatch_aggregate WHERE dispatch_id IN ({marks})",
                tuple(dispatch_ids)).rowcount
            targets = c.execute(
                f"DELETE FROM grape_t_org_dispatch_target WHERE dispatch_id IN ({marks})",
                tuple(dispatch_ids)).rowcount
            snap_rows = c.execute(
                f"DELETE FROM grape_t_org_context_snapshot WHERE dispatch_id IN ({marks})",
                tuple(dispatch_ids)).rowcount
            dispatch_rows = c.execute(
                f"DELETE FROM grape_t_org_dispatch WHERE id IN ({marks})",
                tuple(dispatch_ids)).rowcount
            db.commit()
    # 孤儿快照：批次行已被更早的清理删除，或 dispatch_id 为空的历史快照。
    # 单独按 created_at 清理，避免这些行/目录永远留在库里与磁盘。
    with db_lock:
        c = db.cursor()
        orphan_rows = c.execute(
            "SELECT storage_path FROM grape_t_org_context_snapshot "
            "WHERE COALESCE(created_at, '') < ? "
            "AND (dispatch_id='' OR dispatch_id NOT IN "
            "     (SELECT id FROM grape_t_org_dispatch))",
            (cutoff,),
        ).fetchall()
        orphan_deleted = c.execute(
            "DELETE FROM grape_t_org_context_snapshot "
            "WHERE COALESCE(created_at, '') < ? "
            "AND (dispatch_id='' OR dispatch_id NOT IN "
            "     (SELECT id FROM grape_t_org_dispatch))",
            (cutoff,),
        ).rowcount
        orphan_targets = c.execute(
            "DELETE FROM grape_t_org_dispatch_target "
            "WHERE COALESCE(created_at, '') < ? AND dispatch_id NOT IN "
            "(SELECT id FROM grape_t_org_dispatch)", (cutoff,)).rowcount
        orphan_aggregates = c.execute(
            "DELETE FROM grape_t_org_dispatch_aggregate "
            "WHERE COALESCE(created_at, '') < ? AND dispatch_id NOT IN "
            "(SELECT id FROM grape_t_org_dispatch)", (cutoff,)).rowcount
        db.commit()
    dirs = _remove_snapshot_dirs(
        [r[0] for r in snapshots] + [r[0] for r in orphan_rows])
    result = {
        "dispatch_ids": dispatch_rows,
        "targets": int(targets or 0) + int(orphan_targets or 0),
        "aggregates": int(aggregates or 0) + int(orphan_aggregates or 0),
        "snapshots": int(snap_rows or 0) + int(orphan_deleted or 0),
        "snapshot_dirs": dirs,
        "cutoff": cutoff,
        "retention_days": days,
    }
    if dispatch_rows or snap_rows:
        log.info(f"org retention purged: {result}")
    return result


def maybe_purge_expired_org_records() -> dict:
    """sweeper 内节流调用：距上次清理不足间隔则跳过（默认每天一次）。"""
    global _last_purge_at
    if _RETENTION_INTERVAL_SECONDS <= 0:
        return {"skipped": True, "reason": "interval_disabled"}
    now = time.time()
    if now - _last_purge_at < _RETENTION_INTERVAL_SECONDS:
        return {"skipped": True, "reason": "not_due"}
    _last_purge_at = now
    return purge_expired_org_records()
