"""JWT 工具——双 token 机制（access + refresh）+ 密钥轮换（kid）。

集群多实例共享：所有实例配同一个 JWT_SECRET 环境变量。

密钥轮换（kid）：
  - 生产环境通过 JWT_SECRET_KEYS 配置多密钥池，格式 "kid1:secret1,kid2:secret2"
  - JWT_KID_CURRENT 指定当前签发密钥的 kid
  - 签发时 payload 加 kid 字段，用对应 secret 签名
  - 验签时先尝试从 payload 取 kid，从密钥池查 secret；无 kid 则 fallback 到 JWT_SECRET（兼容旧 token）
  - 轮换流程：添加新密钥到 JWT_SECRET_KEYS → 修改 JWT_KID_CURRENT → 旧密钥保留 30 天 → 移除

Access Token：
  - 短有效期（15 分钟）
  - 用于 API 请求 Authorization: Bearer ***
  - 存 localStorage（JS 可读）

Refresh Token：
  - 长有效期（7 天滚动续期）+ 绝对上限（30 天）
  - 只用于 POST /auth/refresh 换新的 access_token
  - 存 HttpOnly cookie——XSS 偷不到
  - max_exp：首次签发时写入，refresh 时继承不更新——30 天后强制登录

主动撤销：
  - refresh_token 里存 token_version
  - 改密码/logout 时 token_version + 1 → 旧 token 版本不匹配 → 失效
  - verify 时需要对比数据库的 token_version（在 deps.py/auth.py 里做）
"""
import os
import time
import logging
import secrets
from jose import jwt, JWTError

# JWT_SECRET 从环境变量获取——不提供任何已知默认值（防 token 伪造，fail-closed）。
# 显式配置必须以环境变量 JWT_SECRET 或密钥轮换池（security.json）提供。
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"

# 兼容常量：历史默认文件名，现为空串，不再提供任何可预测回退密钥
_DEFAULT_SECRET = ""

# 密钥轮换（kid）配置
# 从 configs/security.json 读取（通过 security_config 模块）
from src.auth.security_config import get_jwt_secret_keys, get_jwt_kid_current, is_production

_log = logging.getLogger("devops-bot")
_SECRET_KEYS_RAW = get_jwt_secret_keys()
JWT_KID_CURRENT = get_jwt_kid_current()

def _parse_secret_keys(raw: str) -> dict:
    """解析 JWT_SECRET_KEYS 环境变量为 {kid: secret} 字典。"""
    result = {}
    if not raw:
        return result
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            kid, secret = pair.split(":", 1)
            kid = kid.strip()
            secret = secret.strip()
            if kid and secret:
                result[kid] = secret
    return result

# 密钥池：kid → secret
_SECRET_KEYS: dict[str, str] = _parse_secret_keys(_SECRET_KEYS_RAW)

# 当前签发密钥的 kid 和 secret
_SIGNING_KID: str = JWT_KID_CURRENT if JWT_KID_CURRENT in _SECRET_KEYS else ""
_SIGNING_SECRET: str = _SECRET_KEYS.get(_SIGNING_KID, "") if _SIGNING_KID else ""

# 过期时间
ACCESS_TOKEN_TTL = 15 * 60       # 15 分钟
REFRESH_TOKEN_TTL = 7 * 24 * 3600  # 7 天（滚动续期）
REFRESH_MAX_LIFETIME = 30 * 24 * 3600  # 30 天（绝对上限——防 token 永久存活）

# 已知不安全的默认值——生产环境拒绝启动（历史默认/常见占位，显式配置也一并拦截）
_INSECURE_SECRETS = {"", "grape-dev-secret-change-in-production", "change-me-in-production", "grape-dev"}

# 开发/测试模式进程内随机兜底密钥（无 JWT_SECRET/密钥池 时生成，不可预测）
_DEV_EPHEMERAL_SECRET = ""


def _is_strong_secret(secret: str) -> bool:
    """HS256 强密钥判定：非空、非黑名单、长度 >= 32 字节。"""
    return bool(secret) and secret not in _INSECURE_SECRETS and len(secret) >= 32


def _ensure_dev_ephemeral_secret() -> str:
    """开发/测试模式兜底：进程内一次性生成随机密钥。

    每次进程启动生成不同值（重启后旧 token 失效，可接受）；不落盘、不可预测。
    """
    global _DEV_EPHEMERAL_SECRET
    if not _DEV_EPHEMERAL_SECRET:
        _DEV_EPHEMERAL_SECRET = secrets.token_urlsafe(48)
        _log.warning("JWT_SECRET 未配置：使用进程内随机密钥（仅开发/测试；生产环境必须配置 JWT_SECRET 或密钥池）")
    return _DEV_EPHEMERAL_SECRET


def validate_jwt_secret() -> bool:
    """校验签名密钥安全性。生产环境必须返回 True。

    返回 False 当且仅当「无有效密钥且非开发/测试随机兜底态」：
    - 密钥轮换池已配置（kid_current + secret_keys）→ True
    - JWT_SECRET 为强密钥 → True
    - 未配置任何密钥：生产 → False（拒绝启动）；开发/测试 → True（进程内随机兜底）
    """
    if _SIGNING_SECRET and _SIGNING_KID:
        return True
    if _is_strong_secret(JWT_SECRET):
        return True
    return not is_production()


def _get_signing_secret() -> tuple[str, str]:
    """获取签发密钥和对应的 kid。

    返回 (secret, kid)。
    优先使用 kid 密钥池中的密钥；未配置则依次回退：强 JWT_SECRET → 开发随机兜底密钥。
    """
    if _SIGNING_SECRET and _SIGNING_KID:
        return _SIGNING_SECRET, _SIGNING_KID
    if _is_strong_secret(JWT_SECRET):
        return JWT_SECRET, ""
    # 无 kid、无强 JWT_SECRET：开发/测试进程内随机兜底（生产启动门禁已拒绝）
    return _ensure_dev_ephemeral_secret(), ""


def _get_verify_secret(kid: str) -> str | None:
    """根据 kid 获取验签密钥。

    D46 P1-5: 区分"有 kid 但未知"和"无 kid"两种情况。
    - 有 kid 且在密钥池中 → 返回对应 secret
    - 有 kid 但不在密钥池中 → 返回 None（拒绝验签，旧 kid 已被移除）
    - 无 kid（旧 token）→ 依次回退：强 JWT_SECRET → 开发随机兜底密钥
    """
    if kid:
        return _SECRET_KEYS.get(kid)  # 有 kid：从池查，找不到返回 None
    # 无 kid → fallback 到 JWT_SECRET（兼容旧 token）；无非空强值则用开发随机兜底
    if _is_strong_secret(JWT_SECRET):
        return JWT_SECRET
    return _ensure_dev_ephemeral_secret()


def create_access_token(user_id: int, username: str, token_version: int = 0,
                        login_provider: str = "", role: str = "") -> str:
    """签发 access token——短有效期（15 分钟），用于 API 请求。

    access_token 不带 max_exp——30 天上限只管控 refresh 长令牌。
    login_provider：登录来源平台（gitcode/github），用于 _get_user_provider_and_token
    按登录平台优先返回对应 token。密码登录/旧 token 缺省空值，fallback 到现有逻辑。
    role：admin 时置 'admin'，供 require_admin 校验；普通用户缺省空。
    """
    secret, kid = _get_signing_secret()
    payload = {
        "sub": str(user_id),
        "username": username,
        "type": "access",
        "ver": token_version,
        "iat": int(time.time()),
        "exp": int(time.time()) + ACCESS_TOKEN_TTL,
    }
    if kid:
        payload["kid"] = kid
    if login_provider:
        payload["login_provider"] = login_provider
    if role:
        payload["role"] = role
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: int, username: str, token_version: int = 0,
                         max_exp: int = None, login_provider: str = "", role: str = "") -> str:
    """签发 refresh token——长有效期（7 天滚动）+ 绝对上限（30 天）。

    max_exp：绝对过期时间戳（首次签发时 = now + 30 天）。
    refresh 换新 refresh 时继承旧 max_exp 不重新生成。
    到达 max_exp 后强制登录——防止 token 永久存活。
    login_provider：登录来源平台（gitcode/github），滚动续期时继承，
    使刷新后的 access_token 仍携带登录来源。
    role：admin 时置 'admin'；供 /api/admin/refresh 校验。
    """
    secret, kid = _get_signing_secret()
    now = int(time.time())
    if max_exp is None:
        # 首次签发——设置 30 天绝对上限
        max_exp = now + REFRESH_MAX_LIFETIME
    payload = {
        "sub": str(user_id),
        "username": username,
        "type": "refresh",
        "ver": token_version,
        "max_exp": max_exp,  # 绝对上限——refresh 时继承不更新
        "iat": now,
        "exp": now + REFRESH_TOKEN_TTL,  # 滚动续期——每次 refresh 重新算 7 天
    }
    if kid:
        payload["kid"] = kid
    if login_provider:
        payload["login_provider"] = login_provider
    if role:
        payload["role"] = role
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def verify_access_token(token: str) -> dict | None:
    """验证 access token——返回 payload 或 None。

    只验证签名和过期——不查数据库。
    token_version 校验在 deps.py 里做（对比数据库）。
    """
    try:
        # 先解码 header 取 kid（不验签）
        unverified = jwt.get_unverified_claims(token)
        kid = unverified.get("kid", "")
        secret = _get_verify_secret(kid)
        if secret is None:
            return None
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload
    except (JWTError, ValueError, TypeError):
        return None


def verify_refresh_token(token: str) -> dict | None:
    """验证 refresh token——返回 payload 或 None。

    验证签名 + 过期 + max_exp（绝对上限）。
    不查数据库——token_version 校验在 auth.py refresh 端点里做。

    4 个条件全通过才有效：
    1. 签名合法（JWTError 不抛）
    2. type=refresh
    3. exp 未过期（滚动续期 7 天）
    4. max_exp 未到达（绝对上限 30 天）
    """
    try:
        unverified = jwt.get_unverified_claims(token)
        kid = unverified.get("kid", "")
        secret = _get_verify_secret(kid)
        if secret is None:
            return None
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            return None
        # 检查 max_exp 绝对上限
        max_exp = payload.get("max_exp")
        if max_exp and int(time.time()) > int(max_exp):
            return None  # 到达 30 天绝对上限——强制登录
        return payload
    except (JWTError, ValueError, TypeError):
        return None
