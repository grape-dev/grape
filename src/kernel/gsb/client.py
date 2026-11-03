"""Grape Service Bus — 统一服务客户端（GsbClient）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.4.

GsbClient 是域间通信的统一入口：
- 服务发现：环境变量优先 > DB 注册表 > localhost 回退
- 认证：HMAC-SHA256 服务令牌（X-Service-Token 头）
- 重试：httpx HTTPTransport(retries=2)
- 超时：10s 总超时，5s 连接超时

使用方式：
    client = GsbClient("mgmt", caller_name="runtime")
    cfg = client.get("/internal/repos/owner/repo/webhook-config")
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from src.kernel.gsb.registry import discover_service
from src.kernel.auth_sdk.service_token import generate_service_token

# 环境变量优先级高于 DB 注册表（支持静态配置）
_ENV_URL_MAP = {
    "mgmt": "GRAPE_MGMT_URL",
    "runtime": "GRAPE_RUNTIME_URL",
    "insight": "GRAPE_INSIGHT_URL",
}

# 单机模式回退端口
_FALLBACK_PORT = {
    "mgmt": 38080,
    "runtime": 38081,
    "insight": 38083,
}

# URL 缓存 TTL
_URL_CACHE_TTL = 30


class GsbClient:
    """统一域间通信客户端——自动服务发现 + 令牌认证 + 重试。"""

    def __init__(self, service_name: str, caller_name: str = ""):
        self.service_name = service_name
        self.caller_name = caller_name or os.environ.get(
            "GRAPE_SERVICE_NAME", "unknown"
        )
        self._cached_url: str | None = None
        self._cached_at: float = 0
        self._http: Any = None
        if httpx:
            self._http = httpx.Client(
                timeout=httpx.Timeout(10.0, connect=5.0),
                transport=httpx.HTTPTransport(retries=2),
            )

    def _resolve_url(self) -> str:
        """解析服务地址——环境变量优先，DB 注册表兜底，单机回退 localhost。"""
        if self._cached_url and time.time() - self._cached_at < _URL_CACHE_TTL:
            return self._cached_url

        # 1. 环境变量
        env_key = _ENV_URL_MAP.get(self.service_name)
        if env_key:
            url = os.environ.get(env_key)
            if url:
                self._cached_url, self._cached_at = url, time.time()
                return url

        # 2. DB 服务注册表
        try:
            svc = discover_service(self.service_name)
            if svc and svc.get("base_url"):
                self._cached_url, self._cached_at = svc["base_url"], time.time()
                return svc["base_url"]
        except Exception:
            pass

        # 3. 单机模式回退
        port = _FALLBACK_PORT.get(self.service_name, 38080)
        fallback = f"http://localhost:{port}"
        self._cached_url, self._cached_at = fallback, time.time()
        return fallback

    def _headers(self) -> dict:
        """认证头——每次请求重新生成令牌（TTL 60s）。"""
        return {
            "X-Internal-Call": "true",
            "X-Service": self.caller_name,
            "X-Service-Token": generate_service_token(self.caller_name),
        }

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET 请求。"""
        if not self._http:
            raise RuntimeError("httpx not available")
        url = self._resolve_url()
        r = self._http.get(f"{url}{path}", params=params, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json_data: dict | None = None) -> Any:
        """POST 请求。"""
        if not self._http:
            raise RuntimeError("httpx not available")
        url = self._resolve_url()
        r = self._http.post(f"{url}{path}", json=json_data, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def stream_sse(self, path: str):
        """SSE 流式读取——yield dict per event。"""
        if not self._http:
            raise RuntimeError("httpx not available")
        url = self._resolve_url()
        with self._http.stream("GET", f"{url}{path}", headers=self._headers()) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._http:
            self._http.close()
