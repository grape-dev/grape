"""冷静期定时任务（D7，需求 22）。

职责：每天执行一次，查询 status=3（冷静期）且超过 30 天的账号，
分步执行匿名化（幂等设计）。

触发方式：asyncio 定时任务（启动后每 24 小时执行一次）
失败不阻塞服务，打 WARNING 日志
"""
import asyncio
import logging

from src.config import log
from src.session_manager import db, db_lock
from src.mysql_compat import get_db_backend

logger = logging.getLogger(__name__)
_bg_tasks: set[asyncio.Task] = set()

_COOLDOWN_INTERVAL = 86400.0  # 每 24 小时执行一次
_COOLDOWN_DAYS = 30           # 冷静期天数
_INITIAL_DELAY = 300.0        # 启动后 5 分钟首次执行


def _get_expired_accounts() -> list[int]:
    """查询冷静期超 30 天的账号 ID 列表。"""
    try:
        with db_lock:
            c = db.cursor()
            if get_db_backend() == "mysql":
                c.execute(
                    "SELECT id FROM grape_accounts "
                    "WHERE status=3 AND updated_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
                    (_COOLDOWN_DAYS,),
                )
            else:
                import time as _t
                from datetime import datetime, timedelta
                cutoff = (datetime.now() - timedelta(days=_COOLDOWN_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                c.execute(
                    "SELECT id FROM grape_accounts "
                    "WHERE status=3 AND updated_at < ?",
                    (cutoff,),
                )
            return [row[0] for row in c.fetchall()]
    except Exception as e:
        log.warning(f"Cooldown: query expired accounts failed: {e}")
        return []


def _anonymize_account(user_id: int):
    """匿名化账号（单事务，幂等设计）。

    需求 8：Step 1-4 在同一个 with db_lock 事务内，任一失败整体 rollback。
    需求 9：datetime('now') 改为 Python time.strftime。
    Step 5 审计日志在 commit 后独立执行。

    ⚠️ MySQL auto-commit 陷阱：mysql_compat Cursor.execute 对 SELECT 自动 commit，
    会把事务内未提交的写操作提前提交，架空 rollback。
    修复：事务内只做写操作（UPDATE/DELETE/INSERT），读操作在事务外先执行。

    Step 1: 改 status=2（原子标记已处理，幂等：WHERE status=3）
    Step 2: 清除 external_identities 绑定
    Step 3: 删除个人工作空间 + 业务数据
    Step 4: 匿名化字段
    Step 5: 记录审计日志（独立，失败不影响匿名化）
    """
    from src.auth import audit as auth_audit
    import time as _t

    # 事务外预查 organization id（避免 MySQL auto-commit 架空事务）
    org_id = None
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id FROM organization WHERE owner_id=? AND is_personal=1",
                (user_id,),
            )
            org_row = c.fetchone()
            if org_row:
                org_id = org_row[0]
    except Exception:
        pass  # 查询失败不阻断，后续 DELETE 会自然 no-op

    try:
        with db_lock:
            c = db.cursor()
            now = _t.strftime("%Y-%m-%d %H:%M:%S")

            # Step 1: 原子标记已处理（幂等：WHERE status=3）
            c.execute(
                "UPDATE grape_accounts SET status=2, updated_at=? "
                "WHERE id=? AND status=3",
                (now, user_id),
            )
            if c.rowcount == 0:
                return  # 已被处理（幂等跳过）

            # Step 2: 清除第三方绑定
            c.execute(
                "DELETE FROM external_identities WHERE grape_user_id=?",
                (user_id,),
            )

            # Step 3: 删除个人工作空间 + 业务数据（org_id 已在事务外查到）
            if org_id:
                c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
                c.execute("DELETE FROM organization WHERE id=?", (org_id,))
            c.execute("DELETE FROM user_repos WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM agent_tasks WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM chat_agent_session WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM chat_conversations WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM issue WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM user_feedback WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM user_credentials WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM api_tokens WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM session_api_tokens WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM scene_likes WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM skill_likes WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM org_member WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM feishu_onboard_task WHERE user_id=?", (user_id,))

            # Step 4: 匿名化字段
            c.execute(
                "UPDATE grape_accounts SET username=?, email=NULL, "
                "password_hash='', nickname='', avatar_url='', "
                "updated_at=? WHERE id=?",
                (f"deleted_{user_id}", now, user_id),
            )

            db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        log.warning(f"Cooldown: anonymize failed for user {user_id}: {e}")
        return

    # Step 5: 记录审计日志（独立，失败不影响匿名化结果）
    try:
        auth_audit.log_audit("account_anonymized", user_id=user_id,
                             detail={"username": f"deleted_{user_id}"})
    except Exception as e:
        log.warning(f"Cooldown: Step 5 failed for user {user_id}: {e}")

    log.info(f"Cooldown: user {user_id} anonymized")


def _cleanup_login_failures():
    """清理过期的 login_failures 记录（24 小时前）。

    login_failures 表的 clear_failures 只在登录成功或解锁时按 identifier 删除，
    过期的失败记录不会被自动清理，长期运行后表会无限增长。
    """
    import time
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400))
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM login_failures WHERE created_at < ?",
                (cutoff,),
            )
            db.commit()
            deleted = c.rowcount
        if deleted:
            log.info(f"Cooldown: cleaned {deleted} expired login_failures records")
    except Exception as e:
        log.warning(f"Cooldown: cleanup login_failures failed: {e}")


def _cleanup_audit_logs():
    """清理过期审计日志（需求 16：保留期限至少 180 天）。

    审计日志保留 180 天，到期后删除。
    哈希链完整性由 prev_hash/curr_hash 保证，删除旧日志不影响新日志的链式校验
    （新日志的 prev_hash 指向删除前最后一条的 curr_hash，删除后 prev_hash 仍可追溯）。
    """
    import time
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 15552000))  # 180 天
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM audit_logs WHERE created_at < ?",
                (cutoff,),
            )
            db.commit()
            deleted = c.rowcount
        if deleted:
            log.info(f"Cooldown: cleaned {deleted} expired audit_logs records (>180 days)")
    except Exception as e:
        log.warning(f"Cooldown: cleanup audit_logs failed: {e}")


def run_cooldown_cleanup():
    """执行一次冷静期清理 + login_failures + audit_logs 过期记录清理。

    集群分布式锁互斥——同一时间只有一个实例执行清理（复用 src/lock.py）。
    """
    from src.lock import acquire_lock, release_lock

    _LOCK_KEY = "cooldown_cleanup"
    _LOCK_TTL = 600  # 10 分钟（清理任务最长执行时间）

    if not acquire_lock(_LOCK_KEY, ttl=_LOCK_TTL):
        log.info("Cooldown: lock contention, skipped (another instance is running)")
        return

    try:
        # 清理过期的 login_failures 记录
        _cleanup_login_failures()
        # 清理过期的 audit_logs 记录（180 天）
        _cleanup_audit_logs()

        expired = _get_expired_accounts()
        if not expired:
            return
        log.info(f"Cooldown: {len(expired)} accounts to anonymize")
        for uid in expired:
            try:
                _anonymize_account(uid)
            except Exception as e:
                log.warning(f"Cooldown: failed to anonymize user {uid}: {e}")
                # 不因单个账号失败阻断整个任务
                continue
        log.info(f"Cooldown: cleanup done ({len(expired)} processed)")
    finally:
        release_lock(_LOCK_KEY)


async def _cooldown_worker(interval: float = _COOLDOWN_INTERVAL):
    """定时任务 worker。"""
    await asyncio.sleep(_INITIAL_DELAY)  # 启动后延迟首次执行
    while True:
        try:
            run_cooldown_cleanup()
        except Exception as e:
            log.warning(f"Cooldown worker error: {e}")
        await asyncio.sleep(interval)


def start_cooldown_scheduler():
    """启动冷静期定时任务。"""
    t = asyncio.create_task(_cooldown_worker())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    log.info("Cooldown scheduler started")


async def stop_cooldown_scheduler():
    """停止冷静期定时任务。"""
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
