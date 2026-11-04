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

2026-11 补齐（让注册表真正参与路由）：
- 新增 event 服务映射与四进程实际端口回退；
- 连接级故障（ConnectError / ConnectTimeout / ReadTimeout / RemoteProtocolError）
  时自动按 "env → 注册表 → fallback" 的顺序换下一个候选地址重试，把死掉/改端口的
  静态配置兜住；HTTP 状态码错误不重试（保留原有语义，避免掩盖 4xx/5xx）。
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

from src.kernel.config import log
from src.kernel.gsb.registry import discover_service
from src.kernel.auth_sdk.service_token import generate_service_token

# 环境变量优先级高于 DB 注册表（支持静态配置）
_ENV_URL_MAP = {
    "mgmt": "GRAPE_MGMT_URL",
    "event": "GRAPE_EVENT_URL",
    "runtime": "GRAPE_RUNTIME_URL",
    "insight": "GRAPE_INSIGHT_URL",
}

# 四进程模式实际端口（单进程 legacy 由 GRAPE_*_URL 覆盖）
_FALLBACK_PORT = {
    "mgmt": 38081,
    "event": 38082,
    "runtime": 38083,
    "insight": 38084,
}

# URL 缓存 TTL
_URL_CACHE_TTL = 30

# 只有传输层错误才换候选地址重试；HTTPStatusError 属于业务响应，直接抛出
_TRANSPORT_ERRORS: tuple = ()
# 连接阶段错误 = 请求肯定没送到服务端，任何方法都可安全切换候选
_CONNECT_ERRORS: tuple = ()
if httpx:
    _TRANSPORT_ERRORS = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.NetworkError,
    )
    _CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)

# 幂等方法：读超时/协议错误后重放不会产生重复副作用
_IDEMPOTENT_METHODS = ("GET", "HEAD", "OPTIONS")


class GsbClient:
    """统一域间通信客户端——自动服务发现 + 令牌认证 + 重试 + 故障切换。"""

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

    # ---------- 地址解析 ----------

    def _env_url(self) -> str | None:
        env_key = _ENV_URL_MAP.get(self.service_name)
        if not env_key:
            return None
        url = os.environ.get(env_key)
        return url.rstrip("/") if url else None

    def _registry_url(self) -> str | None:
        try:
            svc = discover_service(self.service_name)
            base = (svc or {}).get("base_url")
            return base.rstrip("/") if base else None
        except Exception:  # noqa: BLE001 - 注册表不可用不应阻断调用
            return None

    def _fallback_url(self) -> str:
        port = _FALLBACK_PORT.get(self.service_name, 38080)
        return f"http://127.0.0.1:{port}"

    def _candidates(self) -> list[str]:
        """候选地址，去重且保序：环境变量 → 注册表 → 本机回退。"""
        candidates: list[str] = []
        for url in (self._env_url(), self._registry_url()):
            if url and url not in candidates:
                candidates.append(url)
        fallback = self._fallback_url()
        if fallback not in candidates:
            candidates.append(fallback)
        return candidates

    def _resolve_url(self) -> str:
        """解析服务地址（缓存 30s，仅用于展示/SSE；实际请求会逐个候选重试）。"""
        if self._cached_url and time.time() - self._cached_at < _URL_CACHE_TTL:
            return self._cached_url
        url = self._candidates()[0]
        self._cached_url, self._cached_at = url, time.time()
        return url

    def _headers(self) -> dict:
        """认证头——每次请求重新生成令牌（TTL 60s）。"""
        return {
            "X-Internal-Call": "true",
            "X-Service": self.caller_name,
            "X-Service-Token": generate_service_token(self.caller_name),
        }

    # ---------- 请求 ----------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """按候选地址依次尝试；仅传输层错误切换，HTTP 错误原样抛出。"""
        if not self._http:
            raise RuntimeError("httpx not available")

        candidates = self._candidates()
        last_exc: Exception | None = None
        for idx, base in enumerate(candidates):
            try:
                resp = self._http.request(method, f"{base}{path}", headers=self._headers(), **kwargs)
                if idx > 0:
                    log.info(f"GSB failover: service={self.service_name} used {base} (candidate #{idx + 1})")
                self._cached_url, self._cached_at = base, time.time()
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001
                if _TRANSPORT_ERRORS and isinstance(exc, _TRANSPORT_ERRORS):
                    # 非幂等请求只在“连接期失败”时切换候选：
                    # 读超时/协议错误意味着请求可能已被服务端执行（update_issue_state、
                    # log_obs 等都是写），换地址重放会重复写。
                    if method not in _IDEMPOTENT_METHODS and not isinstance(
                        exc, _CONNECT_ERRORS
                    ):
                        log.warning(
                            f"GSB no failover for {method} {path}: "
                            f"url={base} err={type(exc).__name__} (request may have been applied)"
                        )
                        raise
                    last_exc = exc
                    log.warning(
                        f"GSB transport failed: service={self.service_name} url={base} "
                        f"err={type(exc).__name__}; trying next candidate"
                    )
                    continue
                raise
        raise last_exc if last_exc else RuntimeError(f"GSB request failed: {self.service_name}{path}")

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET 请求。"""
        return self._request("GET", path, params=params).json()

    def post(self, path: str, json_data: dict | None = None) -> Any:
        """POST 请求。"""
        return self._request("POST", path, json=json_data).json()

    def stream_sse(self, path: str):
        """SSE 流式读取——yield dict per event（使用首个可用候选地址）。"""
        if not self._http:
            raise RuntimeError("httpx not available")
        base = self._resolve_url()
        with self._http.stream("GET", f"{base}{path}", headers=self._headers()) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._http:
            self._http.close()