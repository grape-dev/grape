"""密码哈希工具——bcrypt（rounds=12）+ SHA-256 预哈希 + 旧算法兼容。

算法版本通过前缀标识：
- ``bcrypt$...`` — bcrypt 哈希（当前推荐）
- ``sha256$...`` — 旧 SHA-256 哈希（兼容期，登录成功后透明升级）

超过 72 字节的密码先做 SHA-256 预哈希再 bcrypt（避免 bcrypt 72 字节截断）。
"""
import hashlib
import hmac

import bcrypt

from src.config import log

# bcrypt salt rounds — 越大越安全但越慢
_BCRYPT_ROUNDS = 12

# 弱密码黑名单 — OWASP Top + 常见中文用户弱密码
# 参照 Have I Been Pwned / SecLists 常见密码 Top 200 精选
_WEAK_PASSWORDS = frozenset({
    # 英文常见弱密码
    "password", "password1", "password12", "password123",
    "12345678", "123456789", "1234567890", "12345678901",
    "qwerty123", "qwertyuiop", "qwerty1",
    "abc12345", "abcdef12", "abcd1234",
    "iloveyou", "iloveyou1",
    "admin123", "admin1234", "administrator",
    "letmein1", "letmein123",
    "welcome1", "welcome123",
    "monkey123", "monkey1",
    "sunshine1", "sunshine123",
    "princess1", "princess123",
    "football1", "football123",
    "shadow1", "shadow123",
    "master1", "master123",
    "dragon1", "dragon123",
    "login1", "login123",
    "starwars1", "starwars123",
    "trustno1", "trustno12",
    "superman1", "superman123",
    "michael1", "michael123",
    "jennifer1", "jennifer123",
    "passw0rd", "passw0rd1", "p@ssw0rd", "p@ssword",
    "changeme1", "changeme",
    "guest1", "guest123",
    "test1234", "test12345",
    "root1234", "root12345",
    "toor1234",
    # 常见中文用户弱密码
    "11111111", "22222222", "33333333", "44444444",
    "55555555", "66666666", "77777777", "88888888",
    "99999999", "00000000",
    "12341234", "12344321", "11112222",
    "woaini131", "woaini520", "woaini521",
    "52013141", "52113141",
    "asd12345", "asdasd123",
    "z123456789", "a123456789",
    "aa123456789", "ab123456789",
    "qq123456", "qq1234567", "qq12345678",
    "wang1234", "wang12345",
    "li123456", "zhang1234",
    # 键盘序列
    "1q2w3e4r", "1q2w3e4r5t",
    "qazwsx123", "qaz12345",
    "zxcvbnm1", "zxcvbnm12",
})


def _pre_hash(plain: str) -> bytes:
    """超过 72 字节的密码先 SHA-256 预哈希。

    bcrypt 自身限制输入 72 字节，超长密码会被静默截断。
    先 SHA-256 把任意长度密码映射为 64 字节十六进制字符串（64 字节 < 72 字节），
    再 bcrypt，避免截断问题。
    """
    raw = plain.encode("utf-8")
    if len(raw) > 72:
        return hashlib.sha256(raw).hexdigest().encode("utf-8")
    return raw


def hash_password(plain: str) -> str:
    """使用 bcrypt 哈希密码，返回带前缀的哈希字符串。

    返回格式：``bcrypt${bcrypt_hash}``
    """
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(_pre_hash(plain), salt)
    return f"bcrypt${hashed.decode('utf-8')}"


def verify_password(plain: str, stored: str) -> bool:
    """验证密码是否匹配。

    支持三种格式：
    - ``bcrypt$...`` — bcrypt 验证
    - ``sha256$...`` — 旧 SHA-256 验证
    - 无前缀 — 旧 SHA-256 hex 直接比对（user_accounts.password_hash 原始格式）
    """
    if not stored:
        return False

    if stored.startswith("bcrypt$"):
        actual = stored[7:]
        try:
            return bcrypt.checkpw(_pre_hash(plain), actual.encode("utf-8"))
        except (ValueError, TypeError):
            return False

    if stored.startswith("sha256$"):
        actual = stored[7:]
        # P2-5: 常量时间比较，防止时序攻击
        return hmac.compare_digest(
            hashlib.sha256(plain.encode("utf-8")).hexdigest(), actual
        )

    # 无前缀 — 旧 user_accounts.password_hash（SHA-256 hex）
    # P2-5: 常量时间比较
    return hmac.compare_digest(
        hashlib.sha256(plain.encode("utf-8")).hexdigest(), stored
    )


def needs_upgrade(stored: str) -> bool:
    """判断存储的哈希是否需要升级为 bcrypt。"""
    return not stored.startswith("bcrypt$")


def check_password_strength(
    password: str,
    username: str = "",
    email: str = "",
) -> "str | None":
    """检查密码强度，返回错误信息或 None（通过）。

    - 最少 8 字符
    - 最多 128 字符
    - 不得与用户名相同
    - 不得与邮箱相同
    - 不可在弱密码黑名单中
    """
    if len(password) < 8:
        return "密码至少 8 个字符"
    if len(password) > 128:
        return "密码最多 128 个字符"
    if username and password == username:
        return "密码不可与用户名相同"
    if email and password == email:
        return "密码不可与邮箱相同"
    if password.lower() in _WEAK_PASSWORDS:
        return "密码强度不足，请使用更复杂的密码"
    return None
