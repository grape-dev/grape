"""JWT 工具——双 token 机制（access + refresh）。

集群多实例共享：所有实例配同一个 JWT_SECRET 环境变量。

Access Token：
  - 短有效期（15 分钟）
  - 用于 API 请求 Authorization: Bearer header
  - 过期后用 refresh_token 换新的

Refresh Token：
  - 长有效期（7 天，滚动续期）
  - 只用于 POST /auth/refresh 换新的 access_token
  - 存 HttpOnly cookie——XSS 偷不到
  - 过期后需重新登录

主动撤销：
  - refresh_token 里存 token_version
  - 改密码/logout 时 token_version + 1 → 旧 token 版本不匹配 → 失效
  - verify 时需要对比数据库的 token_version（在 deps.py/auth.py 里做）
"""
import os
import time
from jose import jwt, JWTError

# JWT_SECRET 从环境变量获取——所有实例必须配同一个值
_DEFAULT_SECRET = "grape-dev-secret-change-in-production"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEFAULT_SECRET)
JWT_ALGORITHM = "HS256"

# 过期时间
ACCESS_TOKEN_TTL = 15 * 60       # 15 分钟
REFRESH_TOKEN_TTL = 7 * 24 * 3600  # 7 天

# 已知不安全的默认值——生产环境拒绝启动
_INSECURE_SECRETS = {"", _DEFAULT_SECRET, "change-me-in-production"}


def validate_jwt_secret() -> bool:
    """校验 JWT_SECRET 是否安全。生产环境必须返回 True。"""
    return JWT_SECRET not in _INSECURE_SECRETS


def create_access_token(user_id: int, username: str, token_version: int = 0) -> str:
    """签发 access token——短有效期（15 分钟），用于 API 请求。"""
    payload = {
        "sub": str(user_id),
        "username": username,
        "type": "access",
        "ver": token_version,
        "iat": int(time.time()),
        "exp": int(time.time()) + ACCESS_TOKEN_TTL,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: int, username: str, token_version: int = 0) -> str:
    """签发 refresh token——长有效期（7 天），用于刷新 access token。"""
    payload = {
        "sub": str(user_id),
        "username": username,
        "type": "refresh",
        "ver": token_version,
        "iat": int(time.time()),
        "exp": int(time.time()) + REFRESH_TOKEN_TTL,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_access_token(token: str) -> dict | None:
    """验证 access token——返回 payload 或 None。

    只验证签名和过期——不查数据库。
    token_version 校验在 deps.py 里做（对比数据库）。
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload
    except JWTError:
        return None


def verify_refresh_token(token: str) -> dict | None:
    """验证 refresh token——返回 payload 或 None。

    只验证签名和过期——不查数据库。
    token_version 校验在 auth.py refresh 端点里做（对比数据库）。
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            return None
        return payload
    except JWTError:
        return None
