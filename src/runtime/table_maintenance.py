"""Large table maintenance tasks (P2-C, Issue #357).

Cleans up bloated tables to prevent storage growth and query degradation:
- agent_tasks: nullify prompt/result_text/skill_files for completed tasks older than 30 days
- task_messages: delete messages older than 90 days (batched)

Designed to run as a periodic background task with distributed lock
(only one worker executes at a time).
"""
import logging
from src.config import log
from src.session_manager import db, db_lock

logger = logging.getLogger(__name__)

_AGENT_TASKS_RETENTION_DAYS = 30
_TASK_MESSAGES_RETENTION_DAYS = 90
_BATCH_SIZE = 500


def cleanup_old_agent_tasks_fields() -> int:
    """Nullify large text fields (prompt/result_text/skill_files) for completed
    agent_tasks older than 30 days.

    This reduces the agent_tasks table from ~1.58GB (bloated prompt/result fields)
    to a manageable size. The task metadata (status, timing, etc.) is preserved.

    Returns the number of rows updated.
    """
    try:
        from src.mysql_compat import get_db_backend
        is_mysql = get_db_backend() == "mysql"
        time_expr = (
            f"NOW() - INTERVAL {_AGENT_TASKS_RETENTION_DAYS} DAY"
            if is_mysql
            else f"datetime('now', '-{_AGENT_TASKS_RETENTION_DAYS} days')"
        )
        total_updated = 0
        while True:
            with db_lock:
                c = db.cursor()
                # Select IDs of rows to update (batched)
                c.execute(
                    f"SELECT id FROM agent_tasks "
                    f"WHERE status='completed' AND created_at < {time_expr} "
                    f"AND (prompt IS NOT NULL AND prompt != '' OR result_text IS NOT NULL AND result_text != '' OR skill_files IS NOT NULL) "
                    f"LIMIT {_BATCH_SIZE}"
                )
                ids = [r[0] for r in c.fetchall()]
                if not ids:
                    break
                # Nullify large fields for these IDs (prompt is NOT NULL, use empty string)
                placeholders = ",".join("?" * len(ids))
                c.execute(
                    f"UPDATE agent_tasks SET prompt='', result_text='', skill_files=NULL "
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
