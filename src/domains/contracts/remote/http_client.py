"""RemoteProvider 共享 HTTP 客户端——连接池、超时、重试（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.6.

与 GsbClient 的区别：ServiceHttpClient 直接使用 base_url（不走 DB 服务发现），
更轻量，适合 RemoteProvider 简单 HTTP 调用场景。
"""
from __future__ import annotations

import os
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from src.kernel.auth_sdk.service_token import generate_service_token


class ServiceHttpClient:
    """域间 HTTP API 客户端。

    Args:
        service_name: 目标服务名（mgmt / runtime / insight）
        base_url: 基础 URL 或环境变量占位（如 "${GRAPE_MGMT_URL}"）
    """

    def __init__(self, service_name: str, base_url: str | None = None):
        self.service_name = service_name
        # 解析环境变量占位（如 ${GRAPE_MGMT_URL}）
        if base_url and base_url.startswith("${") and base_url.endswith("}"):
            env_key = base_url[2:-1]
            self.base_url = os.environ.get(
                env_key, f"http://localhost:3808{self._port_digit(service_name)}"
            )
        else:
            self.base_url = base_url or f"http://localhost:38080"

        self._http: Any = None
        if httpx:
            self._http = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(10.0, connect=5.0),
                transport=httpx.HTTPTransport(retries=2),
                headers={
                    "X-Internal-Call": "true",
                    "X-Service": os.environ.get("GRAPE_SERVICE_NAME", "unknown"),
                },
            )

    @staticmethod
    def _port_digit(service_name: str) -> str:
        """根据服务名返回端口号最后一位。"""
        return {"mgmt": "0", "runtime": "1", "insight": "3"}.get(service_name, "0")

    def _auth_headers(self) -> dict:
        """认证头——每次请求重新生成令牌。"""
        return {
            "X-Service-Token": generate_service_token(
                os.environ.get("GRAPE_SERVICE_NAME", "unknown")
            ),
        }

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET 请求。"""
        if not self._http:
            raise RuntimeError("httpx not available")
        r = self._http.get(path, params=params, headers=self._auth_headers())
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json_data: dict | None = None) -> Any:
        """POST 请求。"""
        if not self._http:
            raise RuntimeError("httpx not available")
        r = self._http.post(path, json=json_data, headers=self._auth_headers())
        r.raise_for_status()
        return r.json()

    def stream_sse(self, path: str):
        """SSE 流式读取——yield dict per event。"""
        if not self._http:
            raise RuntimeError("httpx not available")
        import json
        with self._http.stream("GET", path, headers=self._auth_headers()) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._http:
            self._http.close()
