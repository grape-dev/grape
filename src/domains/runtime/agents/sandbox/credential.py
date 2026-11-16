"""凭据保护策略（策略模式）。

P0：NullCredentialStrategy——从 env 移除凭据，不注入 sentinel。
    Agent 通过 grape 后端中转 API 访问外部服务（不需要 token）。
P2：SentinelStrategy——AES-256-GCM sentinel + TLS MITM 替换（预留，不在 P0 实现）。

CONNECT 隧道里代理看不到 TLS 加密内容，无法替换 sentinel。
P0 的诚实方案：凭据不进沙箱。
"""
from __future__ import annotations
from typing import Protocol


class CredentialStrategy(Protocol):
    """凭据保护策略——策略模式。

    sandbox 层注入参数，走 create_sandbox(..., credential=...)。
    """

    def protect(self, env: dict, credentials: dict) -> dict:
        """转换 env 中的凭据。

        NullCredentialStrategy：从 env 中移除凭据（不保留明文）。
        SentinelStrategy（P2）：替换为 sentinel token。

        protect() 失败时 fail-closed——抛异常，不降级为保留明文。
        """
        ...

    def resolve(self, sentinel: str, target_host: str) -> str | None:
        """代理层解密 sentinel 还原真实值。

        P0 返回 None（无 sentinel）。
        P2 需要 TLS MITM 才能在代理层替换。
        """
        ...


class NullCredentialStrategy:
    """空凭据策略——从 env 中移除凭据，不保留明文。

    --clearenv 后通过 --setenv 白名单注入，凭据不进沙箱。
    Agent 通过 grape 后端中转 API 访问 GitCode/GitHub（不需要 token）。
    """

    def protect(self, env: dict, credentials: dict) -> dict:
        """从 env 中移除凭据。"""
        for key in credentials:
            env.pop(key, None)
        return env

    def resolve(self, sentinel: str, target_host: str) -> str | None:
        """P0 无 sentinel，返回 None。"""
        return None
