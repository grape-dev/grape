"""Large table maintenance tasks (P2-C, Issue #357; Issue #463 表体积治理).

Cleans up bloated tables to prevent storage growth and query degradation:
- agent_tasks: nullify 10 large fields for completed tasks older than 7 days (#463 P0);
  delete terminal (completed/failed/cancelled) tasks older than 90 days, batched (#463 P1)
- task_messages: delete messages older than 90 days (batched)

Designed to run as a periodic background task with distributed lock
(only one worker executes at a time).
"""
import logging
from src.config import log
from src.session_manager import db, db_lock

logger = logging.getLogger(__name__)

_AGENT_TASKS_RETENTION_DAYS = 7
_AGENT_TASKS_DELETE_RETENTION_DAYS = 90
_TASK_MESSAGES_RETENTION_DAYS = 90
_BATCH_SIZE = 500

# Issue #463 P0：completed 任务需置空的大字段全集（prompt NOT NULL → 置空串，其余置 NULL）
_AGENT_TASKS_CLEANUP_FIELDS = (
    "prompt", "result_text", "skill_files",
    "project_info_files", "project_info_meta", "knowledge_files",
    "env_extra", "errors", "stderr", "usage_json",
)
_AGENT_TASKS_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _time_expr(days: int) -> str:
    """跨后端的时间下界表达式（MySQL / SQLite）。"""
    from src.mysql_compat import get_db_backend
    is_mysql = get_db_backend() == "mysql"
    if is_mysql:
        return f"NOW() - INTERVAL {days} DAY"
    return f"datetime('now', '-{days} days')"


def cleanup_old_agent_tasks_fields() -> int:
    """Nullify large text fields (10 fields, see _AGENT_TASKS_CLEANUP_FIELDS) for
    completed agent_tasks older than 7 days (#463 P0: 30d → 7d, fields 3 → 10).

    This reduces the agent_tasks table volume. The task metadata
    (status, timing, etc.) is preserved until the 90-day row delete (P1).

    Returns the number of rows updated.
    """
    try:
        time_expr = _time_expr(_AGENT_TASKS_RETENTION_DAYS)
        dirty_cond = " OR ".join(
            f"({f} IS NOT NULL AND {f} NOT IN ('', '{{}}'))"
            for f in _AGENT_TASKS_CLEANUP_FIELDS
        )
        set_clause = ", ".join(
            f"{f}=''" if f in ("prompt", "result_text") else f"{f}=NULL"
            for f in _AGENT_TASKS_CLEANUP_FIELDS
        )
        total_updated = 0
        while True:
            with db_lock:
                c = db.cursor()
                # Select IDs of rows to update (batched)
                c.execute(
                    f"SELECT id FROM agent_tasks "
                    f"WHERE status='completed' AND created_at < {time_expr} "
                    f"AND ({dirty_cond}) "
                    f"LIMIT {_BATCH_SIZE}"
                )
                ids = [r[0] for r in c.fetchall()]
                if not ids:
                    break
                # Nullify large fields for these IDs (prompt is NOT NULL, use empty string)
                placeholders = ",".join("?" * len(ids))
                c.execute(
                    f"UPDATE agent_tasks SET {set_clause} "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )
                db.commit()
                total_updated += len(ids)
                if len(ids) < _BATCH_SIZE:
                    break
        if total_updated:
            log.info(f"agent_tasks cleanup: nullified large fields for {total_updated} old completed tasks")
        return total_updated
    except Exception as e:
        log.warning(f"agent_tasks cleanup failed: {e}")
        return 0


def delete_old_agent_tasks() -> int:
    """Delete terminal (completed/failed/cancelled) agent_tasks older than 90 days,
    batched (#463 P1). Dependent task_messages rows are deleted with their task.

    Audit trail is preserved by event.json archives + the events table
    (see cleanup.py); deleting old terminal rows does not lose audit.

    Returns the number of rows deleted.
    """
    try:
        time_expr = _time_expr(_AGENT_TASKS_DELETE_RETENTION_DAYS)
        statuses = ",".join(f"'{s}'" for s in _AGENT_TASKS_TERMINAL_STATUSES)
        total_deleted = 0
        while True:
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT id FROM agent_tasks "
                    f"WHERE status IN ({statuses}) AND created_at < {time_expr} "
                    f"LIMIT {_BATCH_SIZE}"
                )
                ids = [r[0] for r in c.fetchall()]
                if not ids:
                    break
                placeholders = ",".join("?" * len(ids))
                # 先删依赖行（task_messages.task_id → agent_tasks.id）再删主行
                c.execute(f"DELETE FROM task_messages WHERE task_id IN ({placeholders})", ids)
                c.execute(f"DELETE FROM agent_tasks WHERE id IN ({placeholders})", ids)
                db.commit()
                total_deleted += len(ids)
                if len(ids) < _BATCH_SIZE:
                    break
        if total_deleted:
            log.info(f"agent_tasks cleanup: deleted {total_deleted} terminal tasks older than "
                     f"{_AGENT_TASKS_DELETE_RETENTION_DAYS} days")
        return total_deleted
    except Exception as e:
        log.warning(f"agent_tasks delete failed: {e}")
        return 0


def cleanup_old_task_messages() -> int:
    """Delete task_messages older than 90 days (batched).

    task_messages grows continuously (234万行/497MB at analysis time).
    Old messages are not useful for active debugging.

    Returns the number of rows deleted.
    """
    try:
        from src.mysql_compat import get_db_backend
        is_mysql = get_db_backend() == "mysql"
        time_expr = (
            f"NOW() - INTERVAL {_TASK_MESSAGES_RETENTION_DAYS} DAY"
            if is_mysql
            else f"datetime('now', '-{_TASK_MESSAGES_RETENTION_DAYS} days')"
        )
        total_deleted = 0
        while True:
            with db_lock:
                c = db.cursor()
                if is_mysql:
                    # MySQL: DELETE ... LIMIT (MySQL-specific)
                    c.execute(
                        f"DELETE FROM task_messages WHERE created_at < {time_expr} LIMIT {_BATCH_SIZE}"
                    )
                else:
                    # SQLite: use subquery with LIMIT
                    # ⚠️ 子查询用派生表包裹（Issue #376 同类排查）：虽然此分支仅 SQLite
                    #    后端执行，但保持写法统一可避免后续维护误复制到双后端路径；
                    #    派生表包裹 SQLite 3.x 同样支持，语义不变。
                    c.execute(
                        f"DELETE FROM task_messages WHERE id IN ("
                        f"SELECT id FROM (SELECT id FROM task_messages WHERE created_at < {time_expr} "
                        f"LIMIT {_BATCH_SIZE}) t)"
                    )
                deleted = c.rowcount
                db.commit()
                total_deleted += deleted
                if deleted < _BATCH_SIZE:
                    break
        if total_deleted:
            log.info(f"task_messages cleanup: deleted {total_deleted} old messages")
        return total_deleted
    except Exception as e:
        log.warning(f"task_messages cleanup failed: {e}")
        return 0
