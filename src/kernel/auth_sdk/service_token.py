"""域间服务令牌——用于 /internal/* 端点互信认证（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.7.

HMAC-SHA256 签名 + TTL 60s + 常量时间比较，防重放和时序攻击。
单机模式（GRAPE_SERVICE_SECRET 未设置）自动跳过认证。

令牌格式：{source}.{target}.{timestamp}.{hmac_sha256}

使用方式：
    # 生成（客户端）
    from src.kernel.auth_sdk.service_token import generate_service_token
    token = generate_service_token("runtime", target_service="mgmt")

    # 验证（服务端中间件）
    from src.kernel.auth_sdk.service_token import verify_service_token
    ok = verify_service_token(token, expected_target="mgmt")
"""
from __future__ import annotations

import hmac
import hashlib
import os
import time

# 服务密钥（集群模式必须设置；单机模式为空→跳过认证）
SERVICE_SECRET = os.environ.get("GRAPE_SERVICE_SECRET", "")

# 令牌 TTL（秒）
TOKEN_TTL = 60

# 时钟漂移容忍（秒）
CLOCK_SKEW = 5


def generate_service_token(
    service_name: str,
    target_service: str = "",
) -> str:
    """生成短期服务令牌（TTL 60s）。

    Args:
        service_name: 调用方服务名（如 "runtime"）
        target_service: 目标服务名（如 "mgmt"），可选

    Returns:
        令牌字符串 "{source}.{target}.{timestamp}.{hmac_sha256}"；
        单机模式（SERVICE_SECRET 为空）返回空串。
    """
    if not SERVICE_SECRET:
        # 集群模式下密钥未设置——告警（middleware 会 fail-closed 拒绝请求）
        if os.environ.get("GRAPE_MODE", "").strip().lower() == "cluster":
            from src.kernel.config import log
            log.warning(
                "generate_service_token called in cluster mode but "
                "GRAPE_SERVICE_SECRET not set; returning empty token "
                "(requests will be rejected by InternalAuthMiddleware)"
            )
        return ""
    ts = str(int(time.time()))
    payload = f"{service_name}.{target_service}.{ts}"
    sig = hmac.new(
        SERVICE_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_service_token(
    token: str,
    expected_target: str = "",
) -> bool:
    """验证服务令牌（TTL + HMAC + 常量时间比较）。

    Args:
        token: 令牌字符串
        expected_target: 期望的目标服务名（空则不校验）

    Returns:
        True 表示验证通过；单机模式（SERVICE_SECRET 为空）始终返回 True。
    """
    if not SERVICE_SECRET:
        # 单机模式跳过验证；集群模式下 middleware 已 fail-closed 拦截
        return True  # single-mode skip
    if not token or token.count(".") != 3:
        return False
    parts = token.split(".")
    if len(parts) != 4:
        return False
    source, target, ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    age = time.time() - ts
    if age > TOKEN_TTL or age < -CLOCK_SKEW:
        return False
    if expected_target and target != expected_target:
        return False
    payload = f"{source}.{target}.{ts_str}"
    expected_sig = hmac.new(
        SERVICE_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig, expected_sig)
