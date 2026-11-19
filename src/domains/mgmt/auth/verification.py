"""验证码领域服务（对外门面）——限流策略 + 发送/校验编排 + 错误码。

领域分层（同一 ``auth`` 包内）：

- ``verification_code.py``   值对象：验证码生成/HMAC 落库哈希/比对、UTC 时间串换算。纯计算，零 IO。
- ``verification_store.py``  仓储：``verification_codes`` 表的唯一 SQL 落点。
- ``verification.py``（本文件）服务门面：限流策略（60s 间隔 / 3 次每 10 分钟 /
  IP 5 次每 10 分钟）、send/verify 编排、结构化 error_code（CODE_NOT_FOUND /
  CODE_EXPIRED / CODE_USED / CODE_INVALID / CODE_STORE_FAILED），并保留所有
  对外 API 与历史私有助手的名字（其它模块与存量测试依赖它们作为 patch 点）。

验证码按场景隔离：register / reset_password / set_password / change_email /
unlock_account 等。哈希存储（不存明文）。原子标记已使用：``UPDATE ... WHERE used=0``。

口径要点：
- A  UTC 时间串按 UTC 解析（``calendar.timegm``），东八区下不再秒过期。
- H  check_rate_limit 通过后落影子记录，未注册邮箱也能推高邮箱/IP 限流计数。
- I  落库哈希带 kid 前缀，密钥轮换期旧 kid 仍可校验。
- J  校验匹配全部未使用码；失败对所有仍有效行累计 attempts。
- M  邮件已发但落库失败 → 明确返回失败（CODE_STORE_FAILED）。
- Q  target 纳入哈希盐，发往 A 的码不能确认 B。
"""

import random
import time

from src.kernel.config import log

from src.kernel.session_manager import db, db_lock

from src.domains.mgmt.auth.email_service import get_email_service, is_dev_verification_code

from src.domains.mgmt.auth import verification_code as _vo
from src.domains.mgmt.auth.verification_store import (
    VerificationCodeStore,
    VerificationStoreError,
)

CODE_TTL = 600              # 验证码有效期 10 分钟（秒）

CODE_LENGTH = _vo.CODE_LENGTH

MAX_ATTEMPTS = 5            # 验证码验证失败上限

SEND_INTERVAL = 60          # 同一邮箱发送间隔（秒）

MAX_SEND_PER_WINDOW = 3     # 窗口内最大发送次数

MAX_IP_PER_WINDOW = 5       # IP 维度窗口内最大请求次数（需求 19）

WINDOW_SECONDS = 600        # 窗口大小 10 分钟（秒）

SHADOW_MAX_AGE = 3600       # 影子记录（限流占位）老化时间（秒）

CLEANUP_PROBABILITY = 0.1   # 过期清理触发概率（避免每次发送全表扫描）

SCENES = frozenset({
    "register",
    "reset_password",
    "set_password",
    "change_email",
    "change_email_confirm",
    "unlock_account",
    "delete_account",
    "oauth_associate",
})


# ---------------------------------------------------------------------------
# 仓储装配
# ---------------------------------------------------------------------------

def _connection():
    """当前连接句柄。

    刻意在**调用时**从本模块命名空间取 ``db`` / ``db_lock``：存量测试与运维
    工具 patch 的是 ``verification.db``，仓储通过本函数拿连接，patch 点因此
    仍落在本模块上。
    """
    return db, db_lock


_store = VerificationCodeStore(_connection)


# ---------------------------------------------------------------------------
# 值对象门面（历史私有名保留，存量测试/工具直接使用）
# ---------------------------------------------------------------------------

def generate_code() -> str:
    """生成 6 位数字验证码。"""
    return _vo.generate()


def _hash_input(code: str, target: str = "") -> str:
    """构造 HMAC 输入（target 非空时作盐，见 Issue #3469 Q）。"""
    return _vo.VerificationCode(code, target).hmac_input()


def _hmac_digest(secret: str, code: str, target: str = "") -> str:
    return _vo.VerificationCode(code, target).digest(secret)


def _hash_code(code: str, target: str = "") -> str:
    """验证码哈希（P2-8: HMAC-SHA256，用应用密钥做 key，防彩虹表）。

    返回纯 digest（64 hex），不带 kid——kid 由 _stored_hash 负责落库。
    """
    return _vo.VerificationCode(code, target).digest_with_signing_secret()


def _stored_hash(code: str, target: str = "") -> str:
    """落库用哈希：``"{kid}:{digest}"``，无 kid 时退化为 ``"{digest}"``（Issue #3469 I）。"""
    return _vo.VerificationCode(code, target).stored_hash


def _match_stored_hash(stored: str, code: str, target: str = "") -> bool:
    """校验输入码是否命中落库哈希（兼容无 kid 的旧格式）。"""
    return _vo.VerificationCode.matches(stored, code, target)


def _utc_str(ts: float) -> str:
    return _vo.utc_str(ts)


def _parse_utc_str(s: str) -> float:
    """解析入库的 UTC 时间串为 epoch 秒（必须按 UTC 解释，Issue #3469 A）。"""
    return _vo.parse_utc(s)


def _now_str() -> str:
    return _vo.now_str()


def _expires_str(ttl: float = CODE_TTL) -> str:
    return _vo.expires_str(ttl)


# ---------------------------------------------------------------------------
# 仓储访问（fail-closed 策略在这一层，仓储只报错）
# ---------------------------------------------------------------------------

def _count_recent_sends(email: str, window: int = WINDOW_SECONDS, scene: str = "") -> int:
    """统计邮箱在窗口内的发送次数。D46 P1-7: 异常时 fail-closed 返回大值。

    scene 非空时只统计该场景（区分 register/reset_password，避免用户同时
    操作多个场景时窗口提前用完）。
    """
    since = _utc_str(time.time() - window)
    try:
        return _store.count_sends_since(email, scene, since)
    except VerificationStoreError:
        return MAX_SEND_PER_WINDOW + 1  # fail-closed


def _count_recent_ip_requests(ip: str, window: int = WINDOW_SECONDS) -> int:
    """统计 IP 在窗口内的验证码请求次数。D46 P1-7: 异常时 fail-closed 返回大值。"""
    if not ip:
        return 0
    since = _utc_str(time.time() - window)
    try:
        return _store.count_ip_since(ip, since)
    except VerificationStoreError:
        return MAX_IP_PER_WINDOW + 1  # fail-closed


def _last_send_time(email: str, scene: str) -> float:
    """最近一次发送的时间戳（0 表示无记录）。D46 P1-7: 异常时 fail-closed 返回当前时间。"""
    try:
        created_at = _store.last_created_at(email, scene)
    except VerificationStoreError:
        return time.time()  # fail-closed: 认为刚发过
    if not created_at:
        return 0.0
    try:
        # Issue #3469 A：created_at 是 UTC 串，必须按 UTC 解析
        return _parse_utc_str(created_at)
    except Exception:
        return time.time()


def _write_shadow_record(rl_key: str, scene: str, ip: str) -> None:
    """写一条「影子记录」——只用于限流计数，不可用于校验（Issue #3469 H）。

    check_rate_limit 旧实现只读不写，未注册邮箱走该分支时不落任何行，而 IP
    维度计数按 verification_codes 行数统计 → 未注册邮箱的请求永远推不高 IP
    计数（可绕过 IP 限流、且 429/200 可区分邮箱是否已注册）。影子写失败只
    告警，不影响限流结论（fail-open 在计数侧无害）。
    """
    try:
        _store.insert_shadow(rl_key, scene, ip, _now_str())
    except VerificationStoreError as e:
        log.warning(f"check_rate_limit shadow record failed (scene={scene}): {e}")


def _mark_all_used(email: str, scene: str) -> bool:
    """原子作废该 key+scene 下全部未使用验证码。"""
    try:
        return _store.mark_all_used(email, scene)
    except VerificationStoreError as e:
        log.warning(f"Mark all verification codes used failed: {e}")
        return False


def _mark_used(vc_id: int) -> bool:
    """原子标记验证码已使用（UPDATE ... WHERE used=0）。"""
    try:
        return _store.mark_used(vc_id)
    except VerificationStoreError as e:
        log.warning(f"Mark verification code used failed: {e}")
        return False


def _increment_attempts(vc_id: int, current_attempts: int = 0) -> None:
    """原子递增失败次数，超 MAX_ATTEMPTS 标记作废。

    P1-4: 原子 UPDATE ... WHERE used=0，消除先读后写的 TOCTOU。
    Issue #3469 J：上限判断放进 SQL，不再依赖读到的 current_attempts。
    current_attempts 仅为兼容旧调用签名保留，不参与判断。
    """
    try:
        _store.increment_attempts(vc_id, MAX_ATTEMPTS)
    except VerificationStoreError as e:
        log.warning(f"Increment attempts failed: {e}")


def _cleanup_expired() -> None:
    """清理过期验证码（best-effort）。P2-9: 10% 概率执行，避免每次发送全表扫描。

    Issue #3469 H：影子记录不能按 expires_at 立即删——它承载限流计数，过早
    删除会让 60s 间隔/窗口次数检查失效。影子记录按 1 小时前的 created_at 老化清理。
    """
    if random.random() > CLEANUP_PROBABILITY:
        return
    try:
        now = _now_str()
        shadow_cutoff = _utc_str(time.time() - SHADOW_MAX_AGE)
        _store.purge_expired(now, shadow_cutoff)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 限流策略
# ---------------------------------------------------------------------------

def _rate_limit_rejection(rl_key: str, scene: str, ip: str = ""):
    """限流策略判定：放行返回 ``None``，否则返回拒绝响应 dict。

    顺序固定（与历史行为一致）：60s 发送间隔 → 窗口次数（3 次/10 分钟，按
    key+scene）→ IP 维度（5 次/10 分钟）。
    """
    now = time.time()

    # 发送间隔检查（60 秒）
    last = _last_send_time(rl_key, scene)
    if last and (now - last) < SEND_INTERVAL:
        remaining = int(SEND_INTERVAL - (now - last))
        # Issue #3469 复审（错误码小瑕疵）：60s 间隔拒绝必须与"发信失败"区分开，
        # 带 retry_after 让前端能倒计时，而不是把它报成 CODE_SEND_FAILED。
        return {"ok": False, "error_code": "RATE_LIMITED", "retry_after": remaining,
                "message": f"请 {remaining} 秒后再试"}

    # 窗口次数检查（3 次/10 分钟）
    recent = _count_recent_sends(rl_key, scene=scene)
    if recent >= MAX_SEND_PER_WINDOW:
        return {"ok": False, "error_code": "RATE_LIMITED",
                "retry_after": WINDOW_SECONDS,
                "message": "请求过于频繁，请稍后再试"}

    # IP 维度限流（5 次/10 分钟）
    if ip:
        ip_count = _count_recent_ip_requests(ip)
        if ip_count >= MAX_IP_PER_WINDOW:
            # Issue #3469 复审 P2：IP 超限要说成 IP 超限，不能冒充「该邮箱被限流」
            return {"ok": False, "error_code": "IP_LIMITED",
                    "message": "当前网络请求过于频繁，请稍后再试"}

    return None


def check_rate_limit(rl_key: str, scene: str, ip: str = "") -> dict:
    """仅检查限流，不发送验证码。

    BUG-8 修复：用于 forgot_password_send_code 对未注册邮箱也模拟限流检查，
    使已注册和未注册邮箱的限流行为一致，防止通过 429 vs 200 区分邮箱是否已注册。

    Issue #3469 H：检查通过后落一条影子记录，使「不实际发码」的影子路径
    也能推高邮箱/IP 维度计数。调用方全是影子路径（register 已注册分支、
    forgot-password 未注册分支、unlock 账号不存在分支、oauth_associate），
    与 send_code 互斥，不会重复落两条。
    """
    rl_key = (rl_key or "").lower().strip()
    if scene not in SCENES:
        return {"ok": False, "message": f"无效的验证码场景: {scene}"}

    rejection = _rate_limit_rejection(rl_key, scene, ip)
    if rejection is not None:
        return rejection

    # Issue #3469 H：限流检查通过 → 落影子记录，让后续请求看到本次
    _write_shadow_record(rl_key, scene, ip)

    return {"ok": True, "message": "OK"}


def count_sends(key: str, scene: str, window_seconds: int = WINDOW_SECONDS) -> int:
    """统计 key 在窗口内该场景的发送/请求次数。

    Issue #3469 E：供路由做「每用户每小时换绑上限」。
    异常时 fail-closed 返回大值，调用方按「已超限」处理。
    """
    key = (key or "").lower().strip()
    since = _utc_str(time.time() - window_seconds)
    try:
        return _store.count_sends_since(key, scene, since)
    except VerificationStoreError as e:
        log.warning(f"count_sends failed (scene={scene}): {e}")
        return 10 ** 6  # fail-closed


# ---------------------------------------------------------------------------
# 发送 / 校验编排
# ---------------------------------------------------------------------------

def send_code(email: str, scene: str, ip: str = "", rate_limit_key: str = "",
              target: str = "") -> dict:
    """发送验证码。

    返回 dict:
    - ``{"ok": True, "message": "验证码已发送"}`` — 成功
    - ``{"ok": False, "message": "..."}`` — 限流失败 / 发信或落库失败
    - 落库失败额外带 ``error_code="CODE_STORE_FAILED"``（Issue #3469 M）

    rate_limit_key: 限流归属键（默认与 email 相同）。
    邮箱变更场景传当前用户邮箱做限流，验证码发到新邮箱。
    target: 验证码真正要确认的目标邮箱（Issue #3469 Q），非空时纳入哈希盐，
    校验时必须传同一个 target。
    """
    email = email.lower().strip()
    rl_key = (rate_limit_key or email).lower().strip()
    if scene not in SCENES:
        return {"ok": False, "message": f"无效的验证码场景: {scene}"}

    rejection = _rate_limit_rejection(rl_key, scene, ip)
    if rejection is not None:
        return rejection

    # 生成验证码
    code = generate_code()
    code_hash = _stored_hash(code, target)
    expires = _expires_str()

    # P1-5 修复：先发邮件，成功后再写 DB。
    # 旧实现先写 DB 后发邮件——邮件发送失败时验证码已落库，用户收不到邮件但验证码存在。
    # P0-2 修复：send_verification_code 返回 False 时（SMTP/Resend 发送失败）不写 DB。
    try:
        result = get_email_service().send_verification_code(email, code, scene)
        if not result:
            log.warning(f"Email send returned False for {email} scene={scene}")
            return {"ok": False, "error_code": "CODE_SEND_FAILED", "message": "验证码发送失败"}
    except RuntimeError as e:
        log.warning(f"Email service error: {e}")
        return {"ok": False, "message": str(e)}
    except Exception as e:
        log.warning(f"Send verification code email failed: {e}")
        return {"ok": False, "error_code": "CODE_SEND_FAILED", "message": "验证码发送失败"}

    # 邮件发送成功 → 存储验证码到 DB
    try:
        _store.insert(rl_key, code_hash, scene, ip, expires, _now_str())
    except VerificationStoreError as e:
        log.warning(f"Store verification code failed (email already sent): {e}")
        # Issue #3469 M：邮件已发出但落库失败 → 用户拿到的码无法校验，
        # 旧实现仍返回 ok=True（前端当成已发送，用户输码必然失败）。
        # 明确返回失败，让前端提示重试（重新发码会走完整限流+落库流程）。
        return {
            "ok": False,
            "message": "验证码发送失败，请重试",
            "error_code": "CODE_STORE_FAILED",
        }

    # 清理过期验证码（best-effort）
    _cleanup_expired()

    return {"ok": True, "message": "验证码已发送"}


def verify_code(email: str, scene: str, code: str, target: str = "",
                consume: bool = True) -> dict:
    """校验验证码。

    返回 dict:
    - ``{"ok": True}`` — 校验通过
    - ``{"ok": False, "message": "...", "error_code": ...}`` — 校验失败

    Issue #3469 P：四种失败原因用 error_code 区分（message 仍为中文人话）：
    CODE_NOT_FOUND / CODE_EXPIRED / CODE_USED / CODE_INVALID。

    Issue #3469 J：取该 key+scene 下全部未使用行，命中任一未过期行即通过
    （并把该 key+scene 下所有未使用行一并置 used=1）；全部不命中时对所有仍
    有效行各 attempts+1（分别判断上限），避免只废最新行导致旧码继续可爆破。

    Issue #3469 Q：target 非空时参与哈希盐，必须与 send_code 传同一 target。

    consume=False：只判定不消费（Issue #3469 复审：改绑要先验二次因子再消费新码，
    否则密码输错一次就把码烧掉）。命中时不置 used，供调用方在其余前置条件都通过后
    再以 consume=True 调用一次正式消费。
    """
    email = email.lower().strip()

    # 开发环境固定验证码
    if is_dev_verification_code(code):
        log.info(f"Dev verification code used for {email} (scene={scene})")
        # 需求8/16: 固定验证码使用时记录审计日志
        from src.domains.mgmt.auth import audit as auth_audit
        auth_audit.log_audit("dev_verification_code_used",
                             detail={"email": auth_audit.mask_email(email), "scene": scene})
        return {"ok": True}

    # 取该 key+scene 下全部未使用行（不再只取最新一条）
    try:
        rows = _store.active_rows(email, scene)
        if rows:
            had_any = True
        else:
            # 区分「从没发过」与「发过但已用掉/已作废」；影子记录
            # （code_hash='' 的限流占位）不算发过码，避免误判为已使用。
            had_any = _store.has_any_code(email, scene)
    except VerificationStoreError as e:
        log.warning(f"Query verification code failed: {e}")
        return {"ok": False, "message": "验证码校验失败，请稍后重试",
                "error_code": "CODE_INVALID"}

    # 逐行判过期（Issue #3469 A：UTC 串按 UTC 解析）
    now = time.time()
    valid, expired = [], []
    for row in rows:
        try:
            exp_ts = _parse_utc_str(row[3])
        except Exception:
            expired.append(row)  # 时间串坏了按过期处理
            continue
        (valid if now <= exp_ts else expired).append(row)

    # 命中任一未过期行即通过；同时作废该 key+scene 下全部未使用行
    for row in valid:
        if _match_stored_hash(row[1], code, target):
            if not consume:
                # 只判定不消费：调用方还会校验其它前置条件（如二次因子）
                return {"ok": True}
            if not _mark_all_used(email, scene):
                log.warning(f"Consume verification codes failed for {email} scene={scene}")
                return {"ok": False, "message": "验证码校验失败，请稍后重试",
                        "error_code": "CODE_INVALID"}
            return {"ok": True}

    if valid:
        # 有未过期行但都不匹配 → 错码。对所有仍有效行各 attempts+1（J）
        for row in valid:
            _increment_attempts(row[0], row[2])
        return {"ok": False, "message": "验证码错误", "error_code": "CODE_INVALID"}

    if expired:
        # 只剩过期行：顺手作废，避免每次校验重复扫描
        _mark_all_used(email, scene)
        return {"ok": False, "message": "验证码已过期，请重新获取",
                "error_code": "CODE_EXPIRED"}

    if had_any:
        return {"ok": False, "message": "验证码已使用，请重新获取",
                "error_code": "CODE_USED"}

    return {"ok": False, "message": "验证码无效或已过期",
            "error_code": "CODE_NOT_FOUND"}