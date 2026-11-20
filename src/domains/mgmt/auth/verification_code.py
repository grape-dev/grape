"""验证码值对象——生成、哈希、比对、UTC 时间换算。

本模块是**纯计算**：零 IO、零 SQL、不 import 数据库。它把验证码链路里所有
「跟存储无关、只跟密码学与时序有关」的规则收敛成两个值对象：

- :class:`VerificationCode`  一次验证码（码值 + target 盐），负责
  ``kid:digest`` 落库哈希与校验比对（含旧格式回退）。
- :class:`UtcTimestamp`      落库用的 UTC 墙钟时间串（秒粒度），负责
  ``str`` <-> ``epoch`` 换算与过期判断。

Issue #3469 的口径都固化在这里：
- A  时间串统一 ``time.gmtime()`` 写、``calendar.timegm`` 读（不得用 mktime）。
- I  落库哈希带 kid 前缀，密钥轮换期旧 kid 仍可校验。
- Q  target（真正要确认的目标邮箱）参与 HMAC 盐，发往 A 的码不能确认 B。
"""

import calendar
import hashlib
import hmac
import secrets
import time

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

CODE_LENGTH = 6


# ---------------------------------------------------------------------------
# UTC 墙钟时间值对象
# ---------------------------------------------------------------------------

class UtcTimestamp:
    """落库用 UTC 时间值对象（秒粒度，格式 ``%Y-%m-%d %H:%M:%S``）。

    写用 :meth:`format`（基于 ``time.gmtime``），读用 :meth:`parse`
    （基于 ``calendar.timegm``）。两者必须同为 UTC：用 ``time.mktime``
    解析会按本地时区解释，东八区下 expires_at 提前 8 小时 → 验证码秒过期
    （Issue #3469 A）。
    """

    __slots__ = ("epoch",)

    def __init__(self, epoch: float):
        self.epoch = float(epoch)

    @classmethod
    def now(cls, offset_seconds: float = 0.0) -> "UtcTimestamp":
        return cls(time.time() + offset_seconds)

    @classmethod
    def parse(cls, text: str) -> "UtcTimestamp":
        """解析入库的 UTC 时间串；格式非法时抛 ``ValueError``。"""
        return cls(calendar.timegm(time.strptime(text, TIME_FORMAT)))

    def format(self) -> str:
        return time.strftime(TIME_FORMAT, time.gmtime(self.epoch))

    def is_after(self, other: "UtcTimestamp") -> bool:
        return self.epoch > other.epoch

    def is_past(self) -> bool:
        return time.time() > self.epoch

    def __float__(self) -> float:
        return self.epoch

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"UtcTimestamp({self.format()!r})"


def utc_str(epoch: float) -> str:
    """epoch 秒 → 落库 UTC 串。"""
    return UtcTimestamp(epoch).format()


def parse_utc(text: str) -> float:
    """落库 UTC 串 → epoch 秒（按 UTC 解释，Issue #3469 A）。"""
    return UtcTimestamp.parse(text).epoch


def now_str() -> str:
    return UtcTimestamp.now().format()


def expires_str(ttl_seconds: float) -> str:
    return UtcTimestamp.now(ttl_seconds).format()


# ---------------------------------------------------------------------------
# HMAC 密钥
# ---------------------------------------------------------------------------

def signing_secret():
    """当前签名密钥 ``(secret, kid)``。

    复用 JWT 有效签名密钥（强 JWT_SECRET 或轮换池；开发/测试回退到进程内
    随机兜底密钥），不保留任何硬编码默认值（沿用 JWT 门禁语义）。
    """
    from src.kernel.auth_sdk.jwt_utils import _get_signing_secret
    return _get_signing_secret()


def verify_secret(kid: str):
    """按 kid 从轮换池取校验密钥；kid 为空/已下线时返回 ``None``。"""
    if not kid:
        return None
    from src.kernel.auth_sdk.jwt_utils import _get_verify_secret
    return _get_verify_secret(kid)


# ---------------------------------------------------------------------------
# 验证码值对象
# ---------------------------------------------------------------------------

def generate() -> str:
    """生成 6 位数字验证码。"""
    return f"{secrets.randbelow(1000000):06d}"


class VerificationCode:
    """一次验证码：``code`` + ``target`` 盐。

    ``target`` 非空时纳入 HMAC 输入（Issue #3469 Q）；为空时沿用原输入，
    保证旧调用方/旧数据语义不变（向后兼容）。
    """

    __slots__ = ("code", "target")

    def __init__(self, code: str, target: str = ""):
        self.code = code
        self.target = (target or "").strip().lower()

    @classmethod
    def generate(cls, target: str = "") -> "VerificationCode":
        return cls(generate(), target)

    # -- 哈希 ---------------------------------------------------------
    def hmac_input(self) -> str:
        return f"{self.target}\x00{self.code}" if self.target else self.code

    def digest(self, secret: str) -> str:
        """用给定密钥算 HMAC-SHA256 digest（64 hex）。"""
        return hmac.new(
            secret.encode("utf-8"), self.hmac_input().encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def digest_with_signing_secret(self) -> str:
        """用当前签名密钥算纯 digest（不带 kid）。"""
        return self.digest(signing_secret()[0])

    @property
    def stored_hash(self) -> str:
        """落库哈希：``"{kid}:{digest}"``，无 kid 时退化为 ``"{digest}"``。

        Issue #3469 I：表里只存 digest 时，密钥轮换后旧验证码全部无法校验
        （10 分钟有效期内也会被无谓作废）。把签发时使用的 kid 一并落库，
        校验时按 kid 取对应密钥，支持轮换期并行校验。
        """
        secret, kid = signing_secret()
        digest = self.digest(secret)
        return f"{kid}:{digest}" if kid else digest

    # -- 比对 ---------------------------------------------------------
    @classmethod
    def matches(cls, stored: str, code: str, target: str = "") -> bool:
        """校验输入码是否命中落库哈希（兼容无 kid 的旧格式）。"""
        if not stored:
            return False
        if ":" in stored:
            kid, _, digest = stored.partition(":")
        else:
            kid, digest = "", stored
        secret = verify_secret(kid)
        if not secret:
            # 旧格式（无 kid）或 kid 已从密钥池移除：回退当前签名密钥。
            secret = signing_secret()[0]
        return hmac.compare_digest(cls(code, target).digest(secret), digest)