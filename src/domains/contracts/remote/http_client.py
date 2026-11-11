"""RemoteProvider 共享 HTTP 客户端——连接池、超时、重试（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.6.

ServiceHttpClient 委托 GsbClient 实现统一的域间通信：
- 服务发现（环境变量 > DB 注册表 > localhost 回退）
- HMAC-SHA256 令牌认证
- httpx 连接池 + 重试 + 超时

与 GsbClient 的区别：ServiceHttpClient 面向 RemoteProvider，
以 service_name 构造，自动解析目标服务地址。
"""
from __future__ import annotations

from typing import Any

from src.kernel.gsb.client import GsbClient


class ServiceHttpClient:
    """域间 HTTP API 客户端——委托 GsbClient 统一实现。

    Args:
        service_name: 目标服务名（mgmt / runtime / insight）
        base_url: 显式 base_url（可选；省略则走 GsbClient 服务发现）
    """

    def __init__(self, service_name: str, base_url: str | None = None):
        self.service_name = service_name
        self._client = GsbClient(
            service_name,
            caller_name=__import__("os").environ.get("GRAPE_SERVICE_NAME", "unknown"),
        )
        # 显式 base_url 覆盖 GsbClient 服务发现
        if base_url:
            resolved = base_url
            # 解析环境变量占位（如 ${GRAPE_MGMT_URL}）
            if base_url.startswith("${") and base_url.endswith("}"):
                env_key = base_url[2:-1]
                import os
                resolved = os.environ.get(env_key, f"http://localhost:{_DEFAULT_PORT.get(service_name, 38080)}")
            self._client._cached_url = resolved
            self._client._cached_at = float("inf")  # 永不过期

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET 请求。"""
        return self._client.get(path, params=params)

    def post(self, path: str, json_data: dict | None = None) -> Any:
        """POST 请求。"""
        return self._client.post(path, json_data=json_data)

    def stream_sse(self, path: str):
        """SSE 流式读取——yield dict per event。"""
        yield from self._client.stream_sse(path)

    def close(self) -> None:
        """关闭 HTTP 客户端。"""
        self._client.close()


_DEFAULT_PORT = {
    "mgmt": 38080,
    "runtime": 38082,
    "insight": 38083,
}
