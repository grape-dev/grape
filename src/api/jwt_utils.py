"""JWT 工具——签发和验证无状态会话 token。

集群多实例共享：所有实例配同一个 JWT_SECRET 环境变量。
"""
import os
import time
from jose import jwt, JWTError

# JWT_SECRET 从环境变量获取——所有实例必须配同一个值
_DEFAULT_SECRET = "grape-dev-secret-change-in-production"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEFAULT_SECRET)
JWT_ALGORITHM = "HS256"
JWT_TTL = 86400  # 24 小时（秒）

# 已知不安全的默认值——生产环境拒绝启动
_INSECURE_SECRETS = {"", _DEFAULT_SECRET, "change-me-in-production"}


def validate_jwt_secret() -> bool:
    """校验 JWT_SECRET 是否安全。生产环境必须返回 True。"""
    return JWT_SECRET not in _INSECURE_SECRETS


def create_jwt_token(user_id: int, username: str) -> str:
    """签发 JWT token——含 user_id + username + 过期时间。"""
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_TTL,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_jwt_token(token: str) -> dict | None:
    """验证 JWT token——返回 payload 或 None。

    任何实例都能验证——不需要查数据库、不查进程内内存。
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        return None
