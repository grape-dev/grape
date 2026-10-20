"""验证码服务——生成、发送、校验、限流。

验证码按场景隔离：register / reset_password / set_password / change_email / unlock_account。
存储在 verification_codes 表，哈希存储（不存明文）。
限流：邮箱维度（60s/次，3次/10min）+ IP 维度（10次/10min）。
原子标记已使用：UPDATE ... SET used=1 WHERE id=? AND used=0。
"""
import hashlib
import hmac
import secrets
import time

from src.config import log
from src.session_manager import db, db_lock
from src.auth.email_service import get_email_service, is_dev_verification_code

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CODE_TTL = 600              # 验证码有效期 10 分钟（秒）
CODE_LENGTH = 6
MAX_ATTEMPTS = 5            # 验证码验证失败上限
SEND_INTERVAL = 60          # 同一邮箱发送间隔（秒）
MAX_SEND_PER_WINDOW = 3     # 窗口内最大发送次数
MAX_IP_PER_WINDOW = 5       # IP 维度窗口内最大请求次数（需求 19）
WINDOW_SECONDS = 600        # 窗口大小 10 分钟（秒）

SCENES = frozenset({
    "register",
    "reset_password",
    "set_password",
    "change_email",
    "unlock_account",
    "delete_account",
    "oauth_associate",
})


# ---------------------------------------------------------------------------
# 验证码生成
# ---------------------------------------------------------------------------

def generate_code() -> str:
    """生成 6 位数字验证码。"""
    return f"{secrets.randbelow(1000000):06d}"


def _hash_code(code: str) -> str:
    """验证码哈希（P2-8: HMAC-SHA256，用应用密钥做 key，防彩虹表）。

    密钥复用 JWT 有效签名密钥（强 JWT_SECRET 或密钥轮换池；开发/测试回退到
    进程内随机兜底密钥）——不保留任何硬编码默认值（沿用 JWT 门禁语义）。
    """
    from src.api.jwt_utils import _get_signing_secret
    key = _get_signing_secret()[0].encode("utf-8")
    return hmac.new(key, code.encode("utf-8"), hashlib.sha256).hexdigest()


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _expires_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + CODE_TTL))


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------

def _count_recent_sends(email: str, window: int = WINDOW_SECONDS, scene: str = "") -> int:
    """统计邮箱在窗口内的发送次数。D46 P1-7: 异常时 fail-closed 返回大值。

    scene 非空时只统计该场景的发送次数（区分 register/reset_password 等场景，
    避免用户同时操作多个场景时窗口提前用完）。
    """
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - window))
    try:
        with db_lock:
            c = db.cursor()
            if scene:
                c.execute(
                    "SELECT COUNT(*) FROM verification_codes "
                    "WHERE email=? AND scene=? AND created_at>=?",
                    (email, scene, since),
                )
            else:
                c.execute(
                    "SELECT COUNT(*) FROM verification_codes "
                    "WHERE email=? AND created_at>=?",
                    (email, since),
                )
            return c.fetchone()[0]
    except Exception:
        return MAX_SEND_PER_WINDOW + 1  # fail-closed


def _count_recent_ip_requests(ip: str, window: int = WINDOW_SECONDS) -> int:
    """统计 IP 在窗口内的验证码请求次数。D46 P1-7: 异常时 fail-closed 返回大值。"""
    if not ip:
        return 0
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - window))
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM verification_codes "
                "WHERE ip=? AND created_at>=?",
                (ip, since),
            )
            return c.fetchone()[0]
    except Exception:
        return MAX_IP_PER_WINDOW + 1  # fail-closed


def _last_send_time(email: str, scene: str) -> float:
    """获取该邮箱该场景最近一次发送的时间戳（0 表示无记录）。D46 P1-7: 异常时 fail-closed 返回当前时间。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT created_at FROM verification_codes "
                "WHERE email=? AND scene=? ORDER BY created_at DESC LIMIT 1",
                (email, scene),
            )
            row = c.fetchone()
        if not row:
            return 0.0
        return time.mktime(time.strptime(row[0], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return time.time()  # fail-closed: 认为刚发过


# ---------------------------------------------------------------------------
# 限流检查
# ---------------------------------------------------------------------------

def check_rate_limit(rl_key: str, scene: str, ip: str = "") -> dict:
    """仅检查限流，不发送验证码、不写 DB。

    BUG-8 修复：用于 forgot_password_send_code 对未注册邮箱也模拟限流检查，
    使已注册和未注册邮箱的限流行为一致，防止通过 429 vs 200 区分邮箱是否已注册。
    """
    rl_key = (rl_key or "").lower().strip()
    if scene not in SCENES:
        return {"ok": False, "message": f"无效的验证码场景: {scene}"}

    # 发送间隔检查（60 秒）
    last = _last_send_time(rl_key, scene)
    if last and (time.time() - last) < SEND_INTERVAL:
        remaining = int(SEND_INTERVAL - (time.time() - last))
        return {"ok": False, "message": f"请 {remaining} 秒后再试"}

    # 窗口次数检查（3 次/10 分钟）
    recent = _count_recent_sends(rl_key, scene=scene)
    if recent >= MAX_SEND_PER_WINDOW:
        return {"ok": False, "message": "请求过于频繁，请稍后再试"}

    # IP 维度限流（10 次/10 分钟）
    if ip:
        ip_count = _count_recent_ip_requests(ip)
        if ip_count >= MAX_IP_PER_WINDOW:
            return {"ok": False, "message": "请求过于频繁，请稍后再试"}

    return {"ok": True, "message": "OK"}


# ---------------------------------------------------------------------------
# 发送验证码
# ---------------------------------------------------------------------------

def send_code(email: str, scene: str, ip: str = "", rate_limit_key: str = "") -> dict:
    """发送验证码。

    返回 dict:
    - ``{"ok": True, "message": "验证码已发送"}`` — 成功
    - ``{"ok": False, "message": "..."}`` — 限流失败

    rate_limit_key: 限流归属键（默认与 email 相同）。
    邮箱变更场景传当前用户邮箱做限流，验证码发到新邮箱。
    """
    email = email.lower().strip()
    rl_key = (rate_limit_key or email).lower().strip()
    if scene not in SCENES:
        return {"ok": False, "message": f"无效的验证码场景: {scene}"}

    # 发送间隔检查（60 秒）— 按限流键
    last = _last_send_time(rl_key, scene)
    if last and (time.time() - last) < SEND_INTERVAL:
        remaining = int(SEND_INTERVAL - (time.time() - last))
        return {"ok": False, "message": f"请 {remaining} 秒后再试"}

    # 窗口次数检查（3 次/10 分钟）— 按限流键 + 场景区分
    recent = _count_recent_sends(rl_key, scene=scene)
    if recent >= MAX_SEND_PER_WINDOW:
        return {"ok": False, "message": "请求过于频繁，请稍后再试"}

    # IP 维度限流（10 次/10 分钟）
    if ip:
        ip_count = _count_recent_ip_requests(ip)
        if ip_count >= MAX_IP_PER_WINDOW:
            return {"ok": False, "message": "请求过于频繁，请稍后再试"}

    # 生成验证码
    code = generate_code()
    code_hash = _hash_code(code)
    expires = _expires_str()

    # P1-5 修复：先发邮件，成功后再写 DB。
    # 旧实现先写 DB 后发邮件——邮件发送失败时验证码已落库，用户收不到邮件但验证码存在。
    # P0-2 修复：send_verification_code 返回 False 时（SMTP/Resend 发送失败）不写 DB。
    try:
        result = get_email_service().send_verification_code(email, code, scene)
        if not result:
            log.warning(f"Email send returned False for {email} scene={scene}")
            return {"ok": False, "message": "验证码发送失败"}
    except RuntimeError as e:
        log.warning(f"Email service error: {e}")
        return {"ok": False, "message": str(e)}
    except Exception as e:
        log.warning(f"Send verification code email failed: {e}")
        return {"ok": False, "message": "验证码发送失败"}

    # 邮件发送成功 → 存储验证码到 DB
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO verification_codes "
                "(email, code_hash, scene, ip, attempts, used, expires_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rl_key, code_hash, scene, ip, 0, 0, expires, _now_str()),
            )
            db.commit()
    except Exception as e:
        log.warning(f"Store verification code failed (email already sent): {e}")
        # 邮件已发但 DB 写入失败——用户会收到邮件但无法验证，可重新发送
        return {"ok": True, "message": "验证码已发送（如未收到请重试）"}

    # 清理过期验证码（best-effort）
    _cleanup_expired()

    return {"ok": True, "message": "验证码已发送"}


# ---------------------------------------------------------------------------
# 校验验证码
# ---------------------------------------------------------------------------

def verify_code(email: str, scene: str, code: str) -> dict:
    """校验验证码。

    返回 dict:
    - ``{"ok": True}`` — 校验通过
    - ``{"ok": False, "message": "..."}`` — 校验失败
    """
    email = email.lower().strip()

    # 开发环境固定验证码
    if is_dev_verification_code(code):
        log.info(f"Dev verification code used for {email} (scene={scene})")
        # 需求8/16: 固定验证码使用时记录审计日志
        from src.auth import audit as auth_audit
        auth_audit.log_audit("dev_verification_code_used",
                             detail={"email": auth_audit.mask_email(email), "scene": scene})
        return {"ok": True}

    try:
        with db_lock:
            c = db.cursor()
            # 取最新未使用未过期的验证码
            c.execute(
                "SELECT id, code_hash, attempts, expires_at FROM verification_codes "
                "WHERE email=? AND scene=? AND used=0 "
                "ORDER BY created_at DESC LIMIT 1",
                (email, scene),
            )
            row = c.fetchone()
    except Exception as e:
        log.warning(f"Query verification code failed: {e}")
        return {"ok": False, "message": "验证码校验失败"}

    if not row:
        return {"ok": False, "message": "验证码无效或已过期"}

    vc_id, stored_hash, attempts, expires_at = row

    # 过期检查
    try:
        exp_ts = time.mktime(time.strptime(expires_at, "%Y-%m-%d %H:%M:%S"))
        if time.time() > exp_ts:
            _mark_used(vc_id)
            return {"ok": False, "message": "验证码无效或已过期"}
    except Exception:
        return {"ok": False, "message": "验证码无效或已过期"}

    # 常量时间比较
    import hmac
    input_hash = _hash_code(code)
    if not hmac.compare_digest(input_hash, stored_hash):
        # 失败 +1
        _increment_attempts(vc_id, attempts)
        return {"ok": False, "message": "验证码无效或已过期"}

    # 原子标记已使用
    if not _mark_used(vc_id):
        return {"ok": False, "message": "验证码无效或已过期"}

    return {"ok": True}


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _mark_used(vc_id: int) -> bool:
    """原子标记验证码已使用（UPDATE ... WHERE used=0）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE verification_codes SET used=1 WHERE id=? AND used=0",
                (vc_id,),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Mark verification code used failed: {e}")
        return False


def _increment_attempts(vc_id: int, current_attempts: int) -> None:
    """原子递增失败次数，超 MAX_ATTEMPTS 标记作废。

    P1-4: 原子 UPDATE SET attempts=attempts+1 WHERE id=? AND used=0
    消除先读后写的 TOCTOU 竞态。current_attempts 仅用于判断是否到上限。
    """
    try:
        new_attempts = current_attempts + 1
        with db_lock:
            c = db.cursor()
            if new_attempts >= MAX_ATTEMPTS:
                # 达到上限：原子标记 used=1 + attempts 递增
                c.execute(
                    "UPDATE verification_codes "
                    "SET attempts = attempts + 1, used = 1 "
                    "WHERE id = ? AND used = 0",
                    (vc_id,),
                )
            else:
                # 未达上限：原子递增 attempts
                c.execute(
                    "UPDATE verification_codes "
                    "SET attempts = attempts + 1 "
                    "WHERE id = ? AND used = 0",
                    (vc_id,),
                )
            db.commit()
    except Exception as e:
        log.warning(f"Increment attempts failed: {e}")


def _cleanup_expired() -> None:
    """清理过期验证码（best-effort）。P2-9: 10% 概率执行，避免每次发送都全表扫描。"""
    import random as _random
    if _random.random() > 0.1:
        return
    try:
        now = _now_str()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM verification_codes WHERE expires_at < ?", (now,))
            db.commit()
    except Exception:
        pass
