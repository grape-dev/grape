"""OAuth token 端点的重试策略（Issue #3054 P1-4）。

把「哪些失败值得再试一次」从交换逻辑里抽出来，避免 `except Exception` 通吃：

- **瞬时**（重试有意义）：超时 / 连接类异常 / 5xx；
- **确定性**（立即返回）：4xx —— ``invalid_client`` / ``redirect_uri_mismatch`` /
  ``invalid_grant`` 重试一万次也是同样结果，而按 RFC 6749 重复出示已用 code
  会撤销先前签发的 token，等于把一次失败放大成「用户被登出」。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略（尝试次数 + 指数退避 + 可重试判定）。"""

    attempts: int = 3                  # 总尝试次数（含首次）
    backoff_base: float = 1.0          # 首次退避秒数
    backoff_factor: float = 2.0        # 退避倍数（1s, 2s, ...）
    server_error_floor: int = 500      # 5xx 起算（含）
    # 5xx 最多尝试次数（含首次）。code 是一次性的：provider 可能在返回 5xx 之前已经
    # 消费并签发 token，重复出示会让 RFC 6749 的授权码被撤销 → 一次失败放大成「被登出」。
    server_error_attempts: int = 2

    #: 判定为瞬时的异常类型（requests 语义：网络层抖动，不是协议层拒绝）
    transient_exceptions: tuple = (
        "Timeout",                 # ConnectTimeout / ReadTimeout 的公共基类
        "ConnectTimeout",
        "ReadTimeout",
        "ConnectionError",         # DNS / 拒绝连接 / 连接被重置
        "ChunkedEncodingError",
        "ContentDecodingError",
    )

    def should_retry_exception(self, exc: BaseException) -> bool:
        """网络层异常是否值得重试（按类型名判定，避免对 requests 硬依赖）。"""
        names = {cls.__name__ for cls in type(exc).__mro__}
        return bool(names & set(self.transient_exceptions))

    def should_retry_status(self, status: int) -> bool:
        """HTTP 状态码是否值得重试：只有 5xx。"""
        return int(status or 0) >= self.server_error_floor

    def should_retry_server_error(self, attempt: int) -> bool:
        """5xx 是否还允许重试（最多 ``server_error_attempts - 1`` 次）。"""
        return attempt + 1 < min(self.attempts, self.server_error_attempts)

    def is_last_attempt(self, attempt: int) -> bool:
        """``attempt`` 从 0 起：是否已用尽尝试次数。"""
        return attempt >= self.attempts - 1

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的退避秒数（1s, 2s, 4s…）。"""
        return self.backoff_base * (self.backoff_factor ** attempt)


#: token 交换使用的默认策略（境外平台网络抖动，两次重试足够）
DEFAULT_TOKEN_RETRY = RetryPolicy()