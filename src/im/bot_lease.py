"""飞书 Bot Lease 分布式锁——保证同一 bot 同一时刻只有一个实例连 WebSocket。

参照 multica Supervisor + LeaseStore 模式：
  - MySQL 行级 CAS 抢锁（lease_token + lease_expires_at）
  - 定期续约（renew）保持锁不过期
  - sweep 扫描过期锁自动接管

参数（config.py）：
  BOT_LEASE_TTL=60             lease 存活时间（秒）
  BOT_LEASE_RENEW_INTERVAL=20  续约间隔（秒）
  BOT_LEASE_SWEEP_INTERVAL=10  sweep 扫描间隔（秒）
  BOT_LEASE_DISABLED=false     true 退回旧模式（无条件连所有 bot）

故障转移：实例挂了 → lease TTL 过期 → 其他实例 sweep 抢锁 → 最慢 70 秒
"""
import secrets
import socket
from src.config import log, BOT_LEASE_TTL, BOT_LEASE_RENEW_INTERVAL, BOT_LEASE_SWEEP_INTERVAL


def generate_instance_id() -> str:
    """生成实例 ID——hostname + 随机 hex，进程启动生成一次。"""
    hostname = socket.gethostname() or "unknown"
    return f"{hostname}-{secrets.token_hex(4)}"


class BotLeaseStore:
    """Bot lease 分布式锁——基于 MySQL CAS。

    所有 SQL 走共享 db_lock（threading.RLock），同步查询。
    在 asyncio 事件循环里调 0.49ms/10s，阻塞可忽略。
    """

    def __init__(self, instance_id: str, ttl: int = BOT_LEASE_TTL,
                 renew_interval: int = BOT_LEASE_RENEW_INTERVAL,
                 sweep_interval: int = BOT_LEASE_SWEEP_INTERVAL):
        self.instance_id = instance_id
        self.ttl = ttl
        self.renew_interval = renew_interval
        self.sweep_interval = sweep_interval
        self._disabled = False  # 问题 M：migration 失败时标记 disabled 退回旧模式

    def try_acquire(self, app_id: str) -> str | None:
        """CAS 抢锁。成功返回 lease_token，失败返回 None。

        条件：lease_token IS NULL（无人持有）OR lease_expires_at < NOW()（已过期）。
        抢到后 lease_expires_at = NOW() + TTL。
        """
        token = secrets.token_hex(16)
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE global_im_bot_config "
                    "SET lease_token = %s, lease_expires_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND), "
                    "    lease_owner = %s, lease_acquired_at = UTC_TIMESTAMP() "
                    "WHERE bot_app_id = %s AND is_active = 1 "
                    "  AND (lease_token IS NULL OR lease_expires_at < UTC_TIMESTAMP())",
                    (token, self.ttl, self.instance_id, app_id),
                )
                db.commit()
                if c.rowcount > 0:
                    log.info(f"[lease] bot {app_id} acquired by {self.instance_id}")
                    return token
                # 竞争失败——正常行为，打 info 不打 warning
                log.info(f"[lease] bot {app_id} acquire failed, held by other instance")
                return None
        except Exception as e:
            # 问题 M：字段不存在等 DB 异常 → 标记 disabled 退回旧模式
            self._disabled = True
            log.error(f"[lease] bot {app_id} acquire error (lease fields may not exist): {e}")
            return None

    def renew(self, app_id: str, token: str) -> bool | None:
        """续约。成功返回 True，CAS 失败返回 False，DB 错误返回 None。

        条件：lease_token = ?（自己的 token 才能续约）。
        续约只更新 lease_expires_at，不更新 lease_acquired_at（保持首次抢锁时间）。
        """
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE global_im_bot_config "
                    "SET lease_expires_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND) "
                    "WHERE bot_app_id = %s AND lease_token = %s AND is_active = 1",
                    (self.ttl, app_id, token),
                )
                db.commit()
                if c.rowcount > 0:
                    log.info(f"[lease] bot {app_id} renewed by {self.instance_id}")
                    return True
                # CAS 失败——lease 被其他实例抢走或 is_active=0
                log.info(f"[lease] bot {app_id} renew failed: CAS mismatch")
                return False
        except Exception as e:
            log.warning(f"[lease] bot {app_id} renew error: {e}")
            return None  # DB 错误返回 None（区分 CAS 失败）

    def release(self, app_id: str, token: str) -> None:
        """释放 lease。只清自己的 token。"""
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE global_im_bot_config "
                    "SET lease_token = NULL, lease_expires_at = NULL, "
                    "    lease_owner = NULL, lease_acquired_at = NULL "
                    "WHERE bot_app_id = %s AND lease_token = %s",
                    (app_id, token),
                )
                db.commit()
                log.info(f"[lease] bot {app_id} released by {self.instance_id}")
        except Exception as e:
            log.warning(f"[lease] bot {app_id} release error: {e}")

    def release_all(self, held_bots: dict[str, str]) -> None:
        """释放所有持有的 lease。held_bots = {app_id: token}"""
        for app_id, token in held_bots.items():
            self.release(app_id, token)

    def list_expired_or_unheld(self, cursor: str = "", limit: int = 500) -> list[dict]:
        """查询可抢的 bot（lease 过期或无人持有）——游标分页。

        返回 [{bot_app_id, bot_type, bot_name, is_active}, ...]
        cursor 为上一次最后一个 bot_app_id，空字符串表示从头开始。
        """
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT bot_app_id, bot_type, bot_name, is_active "
                    "FROM global_im_bot_config "
                    "WHERE is_active = 1 "
                    "  AND (lease_token IS NULL OR lease_expires_at < UTC_TIMESTAMP()) "
                    "  AND bot_app_id > %s "
                    "ORDER BY bot_app_id "
                    "LIMIT %s",
                    (cursor, limit),
                )
                rows = c.fetchall()
                return [
                    {"bot_app_id": r[0], "bot_type": r[1] or "chat_bot",
                     "bot_name": r[2] or "", "is_active": r[3]}
                    for r in rows
                ]
        except Exception as e:
            log.warning(f"[lease] list_expired_or_unheld error: {e}")
            return []

    def batch_renew(self, held_bots: dict[str, str]) -> dict[str, bool]:
        """批量续约所有持有的 lease——一条 SQL 更新所有。

        held_bots = {app_id: token}
        返回 {app_id: success}，False 表示 CAS 失败需要断开。

        一条 UPDATE 按 lease_owner + lease_token 批量更新，
        然后查 DB 确认每个 bot 的 lease_token 是否还是自己的。
        """
        if not held_bots:
            return {}
        results = {}
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                # 批量续约：更新本实例持有的所有 lease
                c.execute(
                    "UPDATE global_im_bot_config "
                    "SET lease_expires_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND) "
                    "WHERE lease_owner = %s AND is_active = 1",
                    (self.ttl, self.instance_id),
                )
                db.commit()
                log.info(f"[lease] batch renewed {c.rowcount} lease(s) by {self.instance_id}")
                # 查 DB 确认每个 bot 的 lease_token 是否还是自己的
                app_ids = list(held_bots.keys())
                placeholders = ",".join(["%s"] * len(app_ids))
                c.execute(
                    f"SELECT bot_app_id, lease_token FROM global_im_bot_config "
                    f"WHERE bot_app_id IN ({placeholders})",
                    app_ids,
                )
                db_tokens = {r[0]: r[1] for r in c.fetchall()}
                for app_id, my_token in held_bots.items():
                    db_token = db_tokens.get(app_id)
                    if db_token is None:
                        # bot 不在 DB 里了（被删了）→ CAS 失败
                        results[app_id] = False
                        log.info(f"[lease] bot {app_id} renew failed: bot deleted")
                    elif db_token != my_token:
                        # lease 被其他实例抢走 → CAS 失败
                        results[app_id] = False
                        log.info(f"[lease] bot {app_id} renew failed: CAS mismatch")
                    else:
                        results[app_id] = True
            return results
        except Exception as e:
            # DB 错误——保持连接等恢复（问题 D）
            log.warning(f"[lease] batch_renew DB error: {e}, keeping all connections")
            for app_id in held_bots:
                results[app_id] = True  # 假设成功，不立即断开
            return results

    def check_inactive_bots(self, held_bots: dict[str, str]) -> list[str]:
        """检查本实例持有但 is_active=0 的 bot——需要断开。

        返回 [app_id, ...]
        """
        if not held_bots:
            return []
        inactive = []
        try:
            from src.session_manager import db, db_lock
            app_ids = list(held_bots.keys())
            placeholders = ",".join(["%s"] * len(app_ids))
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT bot_app_id FROM global_im_bot_config "
                    f"WHERE bot_app_id IN ({placeholders}) AND is_active = 0",
                    app_ids,
                )
                inactive = [r[0] for r in c.fetchall()]
        except Exception as e:
            log.warning(f"[lease] check_inactive_bots error: {e}")
        return inactive

    def get_lease_status(self, app_ids: list[str]) -> dict[str, dict]:
        """查询 bot 的 lease 状态——供 health_check 用。

        返回 {app_id: {lease_owner, lease_expires_at, lease_acquired_at}}
        """
        if not app_ids:
            return {}
        try:
            from src.session_manager import db, db_lock
            placeholders = ",".join(["%s"] * len(app_ids))
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT bot_app_id, lease_owner, lease_expires_at, lease_acquired_at "
                    f"FROM global_im_bot_config WHERE bot_app_id IN ({placeholders})",
                    app_ids,
                )
                return {
                    r[0]: {
                        "lease_owner": r[1] or "",
                        "lease_expires_at": str(r[2]) if r[2] else "",
                        "lease_acquired_at": str(r[3]) if r[3] else "",
                    }
                    for r in c.fetchall()
                }
        except Exception as e:
            log.warning(f"[lease] get_lease_status error: {e}")
            return {}
