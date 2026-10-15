"""登录失败限流——账号维度 + IP 维度。

账号维度：
- 5 次连续失败 → 临时锁定 15 分钟，提供邮箱验证码自助解锁

IP 维度：
- 20 次连续失败 → 临时锁定 15 分钟

P1-2 修复：锁定时长独立于统计窗口。
  旧实现：ACCOUNT_WINDOW=300s（5分钟）统计失败次数，达到阈值后依赖窗口滑动自动解锁，
  实际锁定时间 = 剩余窗口时间（最多5分钟），而非声明的15分钟。
  新实现：锁定时记录 locked_until 时间戳，check_throttle 优先检查 locked_until
  是否在有效期内，而非依赖窗口内计数。
"""
import time

from src.session_manager import db, db_lock
from src.config import log


# 限流参数
ACCOUNT_LOCK_THRESHOLD = 5        # 5 次失败触发锁定
ACCOUNT_LOCK_DURATION = 900       # 锁定 15 分钟（秒）
ACCOUNT_WINDOW = 300              # 账号维度统计窗口 5 分钟（秒）

IP_LOCK_THRESHOLD = 20            # IP 20 次失败触发锁定
IP_LOCK_DURATION = 900            # IP 锁定 15 分钟（秒）
IP_WINDOW = 600                   # IP 维度统计窗口 10 分钟（秒）


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _since_str(window: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - window))


def record_failure(identifier: str, ip: str) -> None:
    """记录一次登录失败。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO login_failures (identifier, ip, created_at) VALUES (?,?,?)",
                (identifier, ip, _now_str()),
            )
            db.commit()
    except Exception as e:
        log.warning(f"Record login failure failed: {e}")


def get_failure_count(identifier: str) -> int:
    """获取账号维度失败次数（窗口内）。"""
    try:
        since = _since_str(ACCOUNT_WINDOW)
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM login_failures "
                "WHERE identifier=? AND created_at>=?",
                (identifier, since),
            )
            return c.fetchone()[0]
    except Exception:
        return 0


def get_ip_failure_count(ip: str) -> int:
    """获取 IP 维度失败次数（窗口内）。"""
    try:
        since = _since_str(IP_WINDOW)
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM login_failures "
                "WHERE ip=? AND created_at>=?",
                (ip, since),
            )
            return c.fetchone()[0]
    except Exception:
        return 0


def check_throttle(identifier: str, ip: str) -> dict:
    """检查是否被限流。

    返回 dict:
    - ``{"allowed": True}`` — 允许登录
    - ``{"allowed": False, "locked": True, "message": "...", "retry_after": N}`` — 账号锁定
    - ``{"allowed": False, "ip_locked": True, "message": "...", "retry_after": N}`` — IP 锁定

    P1-2 修复：锁定时长独立于统计窗口。
    达到阈值后，锁定截止时间 = 第 N 条失败记录时间 + LOCK_DURATION，
    而非依赖窗口内计数滑动自动解锁。
    """
    # IP 维度检查
    ip_count = get_ip_failure_count(ip)
    if ip_count >= IP_LOCK_THRESHOLD:
        # 计算锁定截止时间：第 IP_LOCK_THRESHOLD 条失败记录的时间 + IP_LOCK_DURATION
        lock_until = _get_lock_until(ip, is_ip=True)
        if lock_until and time.time() < lock_until:
            remaining = int(lock_until - time.time())
            return {
                "allowed": False,
                "ip_locked": True,
                "message": f"您的 IP 请求过于频繁，请 {remaining // 60 + 1} 分钟后再试",
                "retry_after": remaining,
            }

    # 账号维度检查
    account_count = get_failure_count(identifier)
    if account_count >= ACCOUNT_LOCK_THRESHOLD:
        # 计算锁定截止时间：第 ACCOUNT_LOCK_THRESHOLD 条失败记录的时间 + ACCOUNT_LOCK_DURATION
        lock_until = _get_lock_until(identifier, is_ip=False)
        if lock_until and time.time() < lock_until:
            remaining = int(lock_until - time.time())
            return {
                "allowed": False,
                "locked": True,
                "message": (
                    f"连续登录失败 {ACCOUNT_LOCK_THRESHOLD} 次，账号已临时锁定 {ACCOUNT_LOCK_DURATION // 60} 分钟。"
                    "可通过邮箱验证码立即解锁。"
                ),
                "retry_after": remaining,
            }

    return {"allowed": True}


def _get_lock_until(identifier: str, is_ip: bool = False) -> float:
    """获取锁定截止时间戳。

    P1-2 修复：取第 N 条（阈值）失败记录的 created_at + LOCK_DURATION。
    如果该时间已过，返回 None（锁定已过期）。

    取第 N 条而不是最后一条，是因为锁定从达到阈值那一刻开始计时，
    不是从最后一次失败开始。

    D46 P1-13 修复：加窗口过滤，防止历史失败记录导致锁定时间计算为过期值。
    """
    threshold = IP_LOCK_THRESHOLD if is_ip else ACCOUNT_LOCK_THRESHOLD
    duration = IP_LOCK_DURATION if is_ip else ACCOUNT_LOCK_DURATION
    window = IP_WINDOW if is_ip else ACCOUNT_WINDOW
    column = "ip" if is_ip else "identifier"

    try:
        with db_lock:
            c = db.cursor()
            since = _since_str(window)
            # 取窗口内第 threshold 条失败记录的时间（按时间升序，跳过 threshold-1 条）
            c.execute(
                f"SELECT created_at FROM login_failures "
                f"WHERE {column}=? AND created_at>=? ORDER BY created_at ASC "
                f"LIMIT 1 OFFSET {threshold - 1}",
                (identifier, since)
            )
            row = c.fetchone()
            if not row:
                return None
            # created_at 格式 "YYYY-MM-DD HH:MM:SS"
            lock_start = time.mktime(time.strptime(row[0], "%Y-%m-%d %H:%M:%S"))
            return lock_start + duration
    except Exception as e:
        log.warning(f"Get lock_until failed: {e}")
        return None


def clear_failures(identifier: str, ip: str = "") -> None:
    """清除失败记录（登录成功或解锁后调用）。

    只清除该 identifier 的失败记录。
    注意：不再清除同 IP 的其他账号失败记录——那会导致同 IP 的攻击者
    失败计数被无辜用户的重置（SEC-5）。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM login_failures WHERE identifier=?",
                (identifier,),
            )
            db.commit()
    except Exception as e:
        log.warning(f"Clear login failures failed: {e}")


def is_account_locked(identifier: str) -> bool:
    """检查账号是否被临时锁定。

    BUG-16 修复：不仅检查失败次数是否达到阈值，还要检查锁定是否已过期。
    锁定过期后应视为未锁定，即使失败记录仍在统计窗口内。
    """
    count = get_failure_count(identifier)
    if count < ACCOUNT_LOCK_THRESHOLD:
        return False
    # 达到阈值，检查锁定是否已过期
    lock_until = _get_lock_until(identifier, is_ip=False)
    if lock_until and time.time() >= lock_until:
        return False  # 锁定已过期
    return True


# ---------------------------------------------------------------------------
# supp-2: OAuth 端点 IP 限流（内存滑动窗口）
# ---------------------------------------------------------------------------
import collections

# IP → deque[timestamps]
_oauth_ip_windows: dict[str, collections.deque] = {}
_oauth_ip_lock = __import__("threading").Lock()

# /login/{provider}: 10 次/分钟
OAUTH_LOGIN_LIMIT = 10
OAUTH_LOGIN_WINDOW = 60

# /auth/exchange: 30 次/分钟
OAUTH_EXCHANGE_LIMIT = 30
OAUTH_EXCHANGE_WINDOW = 60


def check_oauth_rate_limit(ip: str, limit: int, window: int) -> bool:
    """检查 IP 是否超过 OAuth 端点限流阈值。

    返回 True 表示允许（未超限），False 表示被限流。
    内存滑动窗口实现，适合单实例。集群环境可后续迁移到 DB。
    """
    if not ip:
        return True
    now = time.time()
    cutoff = now - window
    with _oauth_ip_lock:
        dq = _oauth_ip_windows.get(ip)
        if dq is None:
            dq = collections.deque()
            _oauth_ip_windows[ip] = dq
        # 清理过期记录
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True
