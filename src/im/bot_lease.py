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
    # 心跳"未来值"容差（秒）：晚于 now+该值的心跳一律不算活（覆盖主机时钟漂移；
    # 历史 CST 写法落在 UTC 未来 +8h，远超此容差 → 被排除，见 _heartbeat_window）
    FUTURE_TOLERANCE_SEC = 60.0

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
        """上报本实例心跳（UPSERT grape_im_instance），供环 owner 计算与接管判定。

        **时间基准必须与读侧一致**：`list_active_instances` 用 Python 的 UTC 计算 cutoff，
        所以这里两个后端都写 **UTC**。历史上 MySQL 分支用 `NOW()`（服务器本地时间，CST=UTC+8），
        导致存进去的值比读侧 cutoff 领先 8 小时 → 所有历史实例行永远"存活" → 一致性哈希环
        被幽灵实例占满 → 真正在跑的实例永远算不到归属、**bot 永远不连接**（IM 与告警通知全哑）。
        改回 UTC 后两个后端与读侧统一。
        """
        try:
            from datetime import datetime, timezone

            from src.session_manager import db, db_lock
            _ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            with db_lock:
                c = db.cursor()
                from src.session_manager import _is_mysql_backend
                if _is_mysql_backend():
                    c.execute(
                        "INSERT INTO grape_im_instance (instance_id, hostname, ip, im_enabled, last_heartbeat) "
                        "VALUES (?,?,?,1,?) "
                        "ON DUPLICATE KEY UPDATE "
                        "last_heartbeat=VALUES(last_heartbeat), hostname=COALESCE(VALUES(hostname), hostname), "
                        "ip=COALESCE(VALUES(ip), ip), im_enabled=1",
                        (self.instance_id, hostname, ip, _ts),
                    )
                else:
                    c.execute(
                        "INSERT INTO grape_im_instance (instance_id, hostname, ip, im_enabled, last_heartbeat) "
                        "VALUES (?,?,?,1,?) "
                        "ON CONFLICT(instance_id) DO UPDATE SET "
                        "last_heartbeat=excluded.last_heartbeat, hostname=COALESCE(excluded.hostname, hostname), "
                        "ip=COALESCE(excluded.ip, ip), im_enabled=1",
                        (self.instance_id, hostname, ip, _ts),
                    )
                db.commit()
        except Exception as e:
            log.warning(f"[lease] heartbeat error: {e}")

    def _heartbeat_window(self, timeout: float, now_dt=None):
        """心跳有效窗口 (下界, 上界)，都是 UTC 口径的本地时间串（各后端可字符串比较）。

        下界 = now - timeout（原有语义：多久没心跳算失联）。
        上界 = now + FUTURE_TOLERANCE_SEC（**2026-10-18 加固**）：历史/未修实例曾用 MySQL 本地时间
        （CST=UTC+8）写 last_heartbeat，那些值在 UTC 视角下"落在未来"，只判下界会让它们继续被当成
        活实例占住一致性哈希环（部署后最长残留 ≈8h+timeout；滚动升级期间未修实例仍在写 CST）。
        60s 容差用于覆盖主机间的正常时钟漂移。
        """
        from datetime import datetime, timezone, timedelta
        now = (now_dt or datetime.now(timezone.utc)).replace(tzinfo=None)
        low = (now - timedelta(seconds=timeout)).strftime("%Y-%m-%d %H:%M:%S")
        high = (now + timedelta(seconds=self.FUTURE_TOLERANCE_SEC)).strftime("%Y-%m-%d %H:%M:%S")
        return low, high

    def list_active_instances(self, timeout: float = 45.0, now_dt=None) -> list[str]:
        """返回心跳存活（last_heartbeat 距今 < timeout）且 im_enabled=1 的实例 id 列表。

        上下界都要判：下界=失联超时，上界=忽略"未来时间"的心跳（CST 口径脏行/未修实例）。
        """
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                # 各后端时间函数统一用 python 侧计算阈值（sqlite datetime / mysql datetime 均兼容）
                _low, _high = self._heartbeat_window(timeout, now_dt)
                c.execute(
                    "SELECT COUNT(*) FROM grape_im_instance WHERE im_enabled=1 AND last_heartbeat > ?",
                    (_high,),
                )
                _future = (c.fetchone() or [0])[0]
                if _future:
                    log.info(f"[lease] 忽略 {_future} 行\"未来时间\"心跳（未修实例写 CST / 历史脏行）")
                c.execute(
                    "SELECT instance_id FROM grape_im_instance "
                    "WHERE im_enabled=1 AND last_heartbeat >= ? AND last_heartbeat <= ?",
                    (_low, _high),
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
            _low, _high = self._heartbeat_window(timeout)
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT 1 FROM grape_im_instance WHERE instance_id=? AND im_enabled=1"
                    " AND last_heartbeat >= ? AND last_heartbeat <= ? LIMIT 1",
                    (owner, _low, _high),
                )
                return c.fetchone() is not None
        except Exception as e:
            log.warning(f"[lease] is_owner_alive error: {e}")
            return False


def purge_stale_instances(older_than_hours: float = 24.0) -> int:
    """清理长期无心跳的实例行（`grape_im_instance` 只增不减，需周期兜底）。

    - 只删 `last_heartbeat` 早于 `now_utc - older_than_hours` 的行；**不删"未来时间"的行**
      （那可能是仍在写 CST 的未修实例；它们的行由读侧上界护栏排除，不影响一致性哈希环）
    - 删除是安全的：写侧是 UPSERT，实例下一次心跳（10s 内）会重建自己
    - 返回删除行数（便于 sweeper 日志与观察）
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(hours=older_than_hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_im_instance WHERE last_heartbeat < ?", (cutoff,))
            n = c.rowcount if isinstance(c.rowcount, int) and c.rowcount > 0 else 0
            db.commit()
        if n:
            log.info(f"[lease] purge {n} 行过期实例心跳（早于 {cutoff}）")
        return n
    except Exception as e:
        _msg = str(e)
        if "no such table" in _msg or "doesn't exist" in _msg:
            log.debug(f"[lease] purge_stale_instances skipped（无此表）: {_msg}")
        else:
            log.warning(f"[lease] purge_stale_instances error: {_msg}")
        return 0
