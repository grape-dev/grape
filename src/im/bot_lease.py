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

    def try_acquire(self, app_id: str, steal_after: int = 60,
                    instance_timeout: float = 45.0) -> str | None:
        token = secrets.token_hex(16)
        expires_at = _future(self.ttl)
        try:
            # 接管保护（cluster-im-lease-stability-design.md §2.3）：
            # lease 虽过期，但旧 owner 心跳存活且刚过期（≤ steal_after）→ 不抢（防活体误抢）；
            # 超过 steal_after 才允许从存活实例强制接管（防 owner 假死卡住）。
            try:
                info = self.lease_owner_info(app_id)
                owner = info.get("owner") or ""
                if owner and owner != self.instance_id and self.is_owner_alive(owner, instance_timeout):
                    exp = (info.get("expires_at") or "").strip()
                    if exp:
                        from datetime import datetime, timezone
                        try:
                            exp_dt = datetime.strptime(exp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            if (datetime.now(timezone.utc) - exp_dt).total_seconds() <= steal_after:
                                return None
                        except ValueError:
                            pass
            except Exception as e:
                log.warning(f"[lease] owner-alive precheck error {app_id}: {e}")
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

    # ── 实例注册表 / 心跳（cluster-im-lease-stability-design.md §2.1）──

    def heartbeat(self, hostname: str = "", ip: str = "") -> None:
        """上报本实例心跳（UPSERT grape_im_instance），供环 owner 计算与接管判定。"""
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                # MySQL 后端无 ON CONFLICT（SQLite 方言），等价翻译为
                # ON DUPLICATE KEY UPDATE（instance_id 唯一键）；excluded.x → VALUES(x)。
                # 实测 2026-10-12：不翻译时 MySQL 1064，心跳每 10s 刷 warning、
                # grape_im_instance 永远空表 → lease 环 owner 计算退化为单实例。
                from src.session_manager import _is_mysql_backend
                if _is_mysql_backend():
                    c.execute(
                        "INSERT INTO grape_im_instance (instance_id, hostname, ip, im_enabled) "
                        "VALUES (?,?,?,1) "
                        "ON DUPLICATE KEY UPDATE "
                        "last_heartbeat=CAST(NOW() AS CHAR), hostname=COALESCE(VALUES(hostname), hostname), "
                        "ip=COALESCE(VALUES(ip), ip), im_enabled=1",
                        (self.instance_id, hostname, ip),
                    )
                else:
                    c.execute(
                        "INSERT INTO grape_im_instance (instance_id, hostname, ip, im_enabled) "
                        "VALUES (?,?,?,1) "
                        "ON CONFLICT(instance_id) DO UPDATE SET "
                        "last_heartbeat=datetime('now'), hostname=COALESCE(excluded.hostname, hostname), "
                        "ip=COALESCE(excluded.ip, ip), im_enabled=1",
                        (self.instance_id, hostname, ip),
                    )
                db.commit()
        except Exception as e:
            log.warning(f"[lease] heartbeat error: {e}")

    def list_active_instances(self, timeout: float = 45.0, now_dt=None) -> list[str]:
        """返回心跳存活（last_heartbeat 距今 < timeout）且 im_enabled=1 的实例 id 列表。"""
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                # 各后端时间函数统一用 python 侧计算阈值（sqlite datetime / mysql datetime 均兼容）
                from datetime import datetime, timezone, timedelta
                cutoff = (now_dt or datetime.now(timezone.utc).replace(tzinfo=None)) - timedelta(seconds=timeout)
                _fmt = cutoff.strftime("%Y-%m-%d %H:%M:%S")
                c.execute(
                    "SELECT instance_id FROM grape_im_instance "
                    "WHERE im_enabled=1 AND last_heartbeat >= ?",
                    (_fmt,),
                )
                return [r[0] for r in c.fetchall()]
        except Exception as e:
            log.warning(f"[lease] list_active_instances error: {e}")
            return []

    def lease_owner_info(self, app_id: str) -> dict:
        """返回某 bot 当前 lease_owner（串 'owner|token'，便于查存活与防误抢）。"""
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT COALESCE(lease_owner,''), COALESCE(lease_token,''), lease_expires_at "
                    "FROM global_im_bot_config WHERE bot_app_id=?",
                    (app_id,),
                )
                r = c.fetchone()
                return {"owner": (r[0] or "") if r else "", "token": (r[1] or "") if r else "",
                        "expires_at": str(r[2]) if r and r[2] else ""}
        except Exception as e:
            log.warning(f"[lease] owner_info error: {e}")
            return {"owner": "", "token": "", "expires_at": ""}

    def is_owner_alive(self, owner: str, timeout: float = 45.0) -> bool:
        """lease 当前 owner 是否在存活实例表中。"""
        if not owner:
            return False
        try:
            from src.session_manager import db, db_lock
            from datetime import datetime, timezone, timedelta
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=timeout)
            _fmt = cutoff.strftime("%Y-%m-%d %H:%M:%S")
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT 1 FROM grape_im_instance WHERE instance_id=? AND im_enabled=1 AND last_heartbeat >= ? LIMIT 1",
                    (owner, _fmt),
                )
                return c.fetchone() is not None
        except Exception as e:
            log.warning(f"[lease] is_owner_alive error: {e}")
            return False
