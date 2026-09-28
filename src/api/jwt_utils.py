"""JWT 工具——双 token 机制（access + refresh）。

集群多实例共享：所有实例配同一个 JWT_SECRET 环境变量。

Access Token：
  - 短有效期（15 分钟）
  - 用于 API 请求 Authorization: Bearer header
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
from jose import jwt, JWTError

# JWT_SECRET 从环境变量获取——所有实例必须配同一个值
_DEFAULT_SECRET = "grape-dev-secret-change-in-production"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEFAULT_SECRET)
JWT_ALGORITHM = "HS256"

# 过期时间
ACCESS_TOKEN_TTL = 15 * 60       # 15 分钟
REFRESH_TOKEN_TTL = 7 * 24 * 3600  # 7 天（滚动续期）
REFRESH_MAX_LIFETIME = 30 * 24 * 3600  # 30 天（绝对上限——防 token 永久存活）

# 已知不安全的默认值——生产环境拒绝启动
_INSECURE_SECRETS = {"", _DEFAULT_SECRET, "change-me-in-production"}


def validate_jwt_secret() -> bool:
    """校验 JWT_SECRET 是否安全。生产环境必须返回 True。"""
    return JWT_SECRET not in _INSECURE_SECRETS


def create_access_token(user_id: int, username: str, token_version: int = 0,
                        login_provider: str = "") -> str:
    """签发 access token——短有效期（15 分钟），用于 API 请求。

    access_token 不带 max_exp——30 天上限只管控 refresh 长令牌。
    login_provider：登录来源平台（gitcode/github），用于 _get_user_provider_and_token
    按登录平台优先返回对应 token。密码登录/旧 token 缺省空值，fallback 到现有逻辑。
    """
    payload = {
        "sub": str(user_id),
        "username": username,
        "type": "access",
        "ver": token_version,
        "iat": int(time.time()),
        "exp": int(time.time()) + ACCESS_TOKEN_TTL,
    }
    if login_provider:
        payload["login_provider"] = login_provider
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: int, username: str, token_version: int = 0,
                         max_exp: int = None, login_provider: str = "") -> str:
    """签发 refresh token——长有效期（7 天滚动）+ 绝对上限（30 天）。

    max_exp：绝对过期时间戳（首次签发时 = now + 30 天）。
    refresh 换新 refresh 时继承旧 max_exp 不重新生成。
    到达 max_exp 后强制登录——防止 token 永久存活。
    login_provider：登录来源平台（gitcode/github），滚动续期时继承，
    使刷新后的 access_token 仍携带登录来源。
    """
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
    if login_provider:
        payload["login_provider"] = login_provider
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

    验证签名 + 过期 + max_exp（绝对上限）。
    不查数据库——token_version 校验在 auth.py refresh 端点里做。

    4 个条件全通过才有效：
    1. 签名合法（JWTError 不抛）
    2. type=refresh
    3. exp 未过期（滚动续期 7 天）
    4. max_exp 未到达（绝对上限 30 天）
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            return None
        # 检查 max_exp 绝对上限
        max_exp = payload.get("max_exp")
        if max_exp and int(time.time()) > int(max_exp):
            return None  # 到达 30 天绝对上限——强制登录
        return payload
    except JWTError:
        return None
