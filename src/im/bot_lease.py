"""飞书 Bot Lease 分布式锁——保证同一 bot 同一时刻只有一个实例连 WebSocket。

基于 SQLite CAS（lease_token + lease_expires_at）。

参数（config.py）：
  BOT_LEASE_TTL=60             lease 存活时间（秒）
  BOT_LEASE_RENEW_INTERVAL=20  续约间隔（秒）
  BOT_LEASE_SWEEP_INTERVAL=10  sweep 扫描间隔（秒）
"""
import secrets, socket, time
from src.config import log, BOT_LEASE_TTL, BOT_LEASE_RENEW_INTERVAL, BOT_LEASE_SWEEP_INTERVAL


def generate_instance_id() -> str:
    hostname = socket.gethostname() or "unknown"
    return f"{hostname}-{secrets.token_hex(4)}"


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _future(seconds: int):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + seconds))


class BotLeaseStore:
    def __init__(self, instance_id: str, ttl: int = BOT_LEASE_TTL,
                 renew_interval: int = BOT_LEASE_RENEW_INTERVAL,
                 sweep_interval: int = BOT_LEASE_SWEEP_INTERVAL):
        self.instance_id = instance_id
        self.ttl = ttl
        self.renew_interval = renew_interval
        self.sweep_interval = sweep_interval
        self._disabled = False

    def try_acquire(self, app_id: str) -> str | None:
        token = secrets.token_hex(16)
        expires_at = _future(self.ttl)
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE global_im_bot_config "
                    "SET lease_token = ?, lease_expires_at = ?, "
                    "    lease_owner = ?, lease_acquired_at = datetime('now') "
                    "WHERE bot_app_id = ? AND is_active = 1 "
                    "  AND (lease_token IS NULL OR lease_expires_at < datetime('now'))",
                    (token, expires_at, self.instance_id, app_id),
                )
                db.commit()
                if c.rowcount > 0:
                    return token
                return None
        except Exception as e:
            self._disabled = True
            log.error(f"[lease] acquire error: {e}")
            return None

    def renew(self, app_id: str, token: str) -> bool | None:
        expires_at = _future(self.ttl)
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE global_im_bot_config "
                    "SET lease_expires_at = ? "
                    "WHERE bot_app_id = ? AND lease_token = ? AND is_active = 1",
                    (expires_at, app_id, token),
                )
                db.commit()
                if c.rowcount > 0:
                    return True
                return False
        except Exception as e:
            log.warning(f"[lease] renew error: {e}")
            return None

    def release(self, app_id: str, token: str) -> None:
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE global_im_bot_config "
                    "SET lease_token = NULL, lease_expires_at = NULL, "
                    "    lease_owner = NULL, lease_acquired_at = NULL "
                    "WHERE bot_app_id = ? AND lease_token = ?",
                    (app_id, token),
                )
                db.commit()
        except Exception as e:
            log.warning(f"[lease] release error: {e}")

    def release_all(self, held_bots: dict[str, str]) -> None:
        for app_id, token in held_bots.items():
            self.release(app_id, token)

    def list_expired_or_unheld(self, cursor: str = "", limit: int = 500) -> list[dict]:
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT bot_app_id, bot_type, bot_name, is_active "
                    "FROM global_im_bot_config "
                    "WHERE is_active = 1 "
                    "  AND (lease_token IS NULL OR lease_expires_at < datetime('now')) "
                    "  AND bot_app_id > ? "
                    "ORDER BY bot_app_id "
                    "LIMIT ?",
                    (cursor, limit),
                )
                return [
                    {"bot_app_id": r[0], "bot_type": r[1] or "chat_bot",
                     "bot_name": r[2] or "", "is_active": r[3]}
                    for r in c.fetchall()
                ]
        except Exception as e:
            log.warning(f"[lease] list_expired error: {e}")
            return []

    def batch_renew(self, held_bots: dict[str, str]) -> dict[str, bool]:
        if not held_bots:
            return {}
        results = {}
        expires_at = _future(self.ttl)
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE global_im_bot_config SET lease_expires_at = ? "
                    "WHERE lease_owner = ? AND is_active = 1",
                    (expires_at, self.instance_id),
                )
                db.commit()
                app_ids = list(held_bots.keys())
                ph = ",".join(["?"] * len(app_ids))
                c.execute(
                    f"SELECT bot_app_id, lease_token FROM global_im_bot_config "
                    f"WHERE bot_app_id IN ({ph})", app_ids,
                )
                db_tokens = {r[0]: r[1] for r in c.fetchall()}
                for app_id, my_token in held_bots.items():
                    db_token = db_tokens.get(app_id)
                    if db_token is None:
                        results[app_id] = False
                    elif db_token != my_token:
                        results[app_id] = False
                    else:
                        results[app_id] = True
            return results
        except Exception as e:
            log.warning(f"[lease] batch_renew error: {e}")
            for app_id in held_bots:
                results[app_id] = True
            return results

    def check_inactive_bots(self, held_bots: dict[str, str]) -> list[str]:
        if not held_bots:
            return []
        inactive = []
        try:
            from src.session_manager import db, db_lock
            app_ids = list(held_bots.keys())
            ph = ",".join(["?"] * len(app_ids))
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT bot_app_id FROM global_im_bot_config "
                    f"WHERE bot_app_id IN ({ph}) AND is_active = 0", app_ids,
                )
                inactive = [r[0] for r in c.fetchall()]
        except Exception as e:
            log.warning(f"[lease] check_inactive error: {e}")
        return inactive

    def get_lease_status(self, app_ids: list[str]) -> dict[str, dict]:
        if not app_ids:
            return {}
        try:
            from src.session_manager import db, db_lock
            ph = ",".join(["?"] * len(app_ids))
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT bot_app_id, lease_owner, lease_expires_at, lease_acquired_at "
                    f"FROM global_im_bot_config WHERE bot_app_id IN ({ph})", app_ids,
                )
                return {
                    r[0]: {"lease_owner": r[1] or "", "lease_expires_at": str(r[2]) if r[2] else "",
                           "lease_acquired_at": str(r[3]) if r[3] else ""}
                    for r in c.fetchall()
                }
        except Exception as e:
            log.warning(f"[lease] get_status error: {e}")
            return {}
