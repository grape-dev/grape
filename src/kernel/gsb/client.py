"""Grape Service Bus — 统一服务客户端（GsbClient）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.4。

GsbClient 是域间通信的统一入口：
- 服务发现：环境变量优先 > DB 注册表（全部实例）> localhost 回退
- 负载均衡：注册表里同一服务有多个实例时，按 GRAPE_GSB_LB 策略挑一个
- 认证：HMAC-SHA256 服务令牌（X-Service-Token 头）
- 重试：httpx HTTPTransport(retries=2)
- 超时：10s 总超时，5s 连接超时

使用方式：
    client = GsbClient("mgmt", caller_name="runtime")
    cfg = client.get("/internal/repos/owner/repo/webhook-config")

2026-11 补充：
- 新增 event 服务映射与四进程实际端口回退；
- 传输层故障时按候选顺序切换地址；非幂等写请求只在"连接期失败"才切换，
  避免读超时后重放造成重复写。
- 多实例负载均衡：discover_service() 只能拿一条，这里改用 list_instances()
  拿全部 ``service`` / ``service@namespace`` 行，按策略排序后依次尝试。
- Issue #2479：metrics status 标签归一化（remote_timeout / remote_unreachable /
  数字业务码），可区分「remote 超时」与「业务错误」；新增 fail-fast 异常
  （GsbRemoteTimeoutError 等，由 provider 路由代理包装，client 保持原始传输异常语义）；
  per-method 超时经 with_timeout() 线程本地覆盖逐请求下发（不改共享客户端超时，
  无 per-method 粘滞）。

负载均衡策略（GRAPE_GSB_LB，默认 local_first）：
    local_first     本机命名空间实例优先，同命名空间内轮询，失败再跨节点（默认，兼容旧行为）
    round_robin     忽略本机偏好，跨全部实例轮询
    random          随机挑，适合实例数多且同质的场景
    least_inflight  选并发在途请求最少的实例

配套开关：
    GRAPE_GSB_NAMESPACE       本机命名空间（默认读不到 → 空，等价"无本机偏好"）
    GRAPE_GSB_LB_COOLDOWN    实例传输失败后的冷却秒数（默认 30）
    GRAPE_GSB_LB_CACHE_TTL   实例列表缓存秒数（默认 5，避免每次调用查库）
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from src.kernel.auth_sdk.service_token import generate_service_token
from src.kernel.config import log
from src.kernel.gsb import metrics as _gsb_metrics
from src.kernel.gsb.registry import list_instances

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

# URL 缓存 TTL（仅用于展示 / SSE 首地址）
_URL_CACHE_TTL = 30

# ---------------- 负载均衡配置 ----------------
_LB_ENV = "GRAPE_GSB_LB"
_LB_DEFAULT = "local_first"
_LB_STRATEGIES = ("local_first", "round_robin", "random", "least_inflight")
_LB_ALIASES = {"local": "local_first", "rr": "round_robin", "least": "least_inflight"}

_NAMESPACE_ENV = "GRAPE_GSB_NAMESPACE"
_COOLDOWN_SECONDS = float(os.environ.get("GRAPE_GSB_LB_COOLDOWN", "30"))
_INSTANCE_CACHE_TTL = float(os.environ.get("GRAPE_GSB_LB_CACHE_TTL", "5"))

# 进程级共享状态：RemoteProvider 每次 get_service() 都会新建 GsbClient，
# 若轮询游标/实例缓存放在实例上，round_robin 会退化成"永远选第一个"。
_STATE_LOCK = threading.Lock()
_INSTANCE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_RR_CURSOR: dict[str, int] = {}


def reset_instance_cache(service: str | None = None) -> None:
    """清空实例列表缓存（测试/配置热更新用）。"""
    with _STATE_LOCK:
        if service is None:
            _INSTANCE_CACHE.clear()
        else:
            _INSTANCE_CACHE.pop(service, None)

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


# ---------------- fail-fast 异常（Issue #2479） ----------------


class GsbRemoteChannelError(RuntimeError):
    """GSB remote 通道失败（超时/不可达）——fail-fast，不回落 lib。

    稳定错误码 ``code`` 供业务侧判别；由 provider 路由代理（_RoutedService）
    在 remote 调用包装传输层异常时抛出。GsbClient 自身保持原始 httpx 异常语义。
    """

    code = "GSB_REMOTE_CHANNEL_ERROR"


class GsbRemoteTimeoutError(GsbRemoteChannelError):
    """remote 调用超时（读/写/连接/池超时）——不回落 lib。"""

    code = "GSB_REMOTE_TIMEOUT"


class GsbRemoteUnreachableError(GsbRemoteChannelError):
    """remote 通道不可达（连接拒绝/协议错/网络错等非超时传输失败）——不回落 lib。"""

    code = "GSB_REMOTE_UNREACHABLE"


def _status_label(exc: Exception) -> str:
    """异常 → metrics status 标签（可区分 remote 超时与业务错误）。

    - httpx 超时族 → ``remote_timeout``；
    - 其它传输层错误 → ``remote_unreachable``；
    - HTTPStatusError 等带响应码的业务错误 → 数字状态码；
    - 其余 → 异常类型名（兜底观测）。
    """
    if httpx and isinstance(exc, httpx.TimeoutException):
        return "remote_timeout"
    if httpx and isinstance(exc, httpx.TransportError):
        return "remote_unreachable"
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return str(code) if code else type(exc).__name__


def resolve_lb_strategy() -> str:
    """读取并归一化 GRAPE_GSB_LB（非法值回退默认策略并告警一次）。"""
    raw = os.environ.get(_LB_ENV, "").strip().lower()
    if not raw:
        return _LB_DEFAULT
    value = _LB_ALIASES.get(raw, raw)
    if value not in _LB_STRATEGIES:
        log.warning(f"invalid {_LB_ENV}={raw!r}, fallback to {_LB_DEFAULT}")
        return _LB_DEFAULT
    return value


def _annotate_gsb_span(**attrs: Any) -> None:
    """把目标实例/状态补到 dispatch 层那条 gsb span 上（无活动 span 时无副作用）。

    dispatch 只看到 service/method/mode，真正解析出来的实例与 HTTP 状态只有这里知道。
    """
    try:
        from src.kernel.otel import annotate_current_span
        annotate_current_span(**attrs)
    except Exception:  # noqa: BLE001 — 埋点失败绝不阻断总线调用
        return


def _instance_of(url: str) -> str:
    """URL → 实例标识（netloc），用于指标与冷却表。"""
    try:
        return urlsplit(url).netloc or url
    except Exception:  # noqa: BLE001
        return url


class GsbClient:
    """统一域间通信客户端——自动服务发现 + 负载均衡 + 令牌认证 + 故障切换。"""

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

        # 负载均衡状态
        self._lock = threading.Lock()
        self._rr_cursor = 0
        self._inflight: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._instances: list[dict] = []
        self._instances_at = 0.0
        # per-request 超时线程本地覆盖（Issue #2479 QA 审查修复 Required#3：
        # 不再改共享 httpx.Client.timeout——那会让 per-method 超时「粘滞」到
        # 后续无配置方法的调用上，产生假 GSB_REMOTE_TIMEOUT）
        self._tls = threading.local()

    # ---------- 地址解析 ----------

    def _env_url(self) -> str | None:
        env_key = _ENV_URL_MAP.get(self.service_name)
        if not env_key:
            return None
        url = os.environ.get(env_key)
        return url.rstrip("/") if url else None

    def _fetch_instances(self) -> list[dict]:
        """从注册表取该服务的全部存活实例（进程级 TTL 缓存）。

        缓存挂在模块级而不是实例上：RemoteProvider 每次调用都会新建 GsbClient，
        实例级缓存等于每次都要查一次库。
        """
        now = time.time()
        with _STATE_LOCK:
            hit = _INSTANCE_CACHE.get(self.service_name)
        if hit and now - hit[0] < _INSTANCE_CACHE_TTL:
            return list(hit[1])
        try:
            rows = list_instances(self.service_name)
        except Exception:  # noqa: BLE001 - 注册表不可用不应阻断调用
            rows = []
        alive = [
            r
            for r in rows
            if (r.get("base_url") or "") and r.get("alive", True)
        ]
        with _STATE_LOCK:
            _INSTANCE_CACHE[self.service_name] = (now, alive)
        return list(alive)

    def _available(self, instances: list[dict]) -> list[dict]:
        """过滤掉处于冷却期的实例；全部冷却时退回全集（宁可重试也不空转）。"""
        now = time.time()
        with self._lock:
            hot = [r for r in instances if self._cooldown_until.get(_instance_of(r["base_url"]), 0) <= now]
        return hot or instances

    def _ordered_instances(self) -> list[dict]:
        """按 GRAPE_GSB_LB 策略给实例排序（返回的列表就是要尝试的顺序）。"""
        instances = self._available(self._fetch_instances())
        if not instances:
            return []

        strategy = resolve_lb_strategy()
        ns = os.environ.get(_NAMESPACE_ENV, "").strip()

        if strategy == "random":
            shuffled = list(instances)
            random.shuffle(shuffled)
            return shuffled

        if strategy == "least_inflight":
            with self._lock:
                return sorted(
                    instances,
                    key=lambda r: (
                        self._inflight.get(_instance_of(r["base_url"]), 0),
                        r.get("age_seconds", 0),
                    ),
                )

        # round_robin / local_first：游标必须进程级共享，否则 new GsbClient 每次归零
        with _STATE_LOCK:
            cursor = _RR_CURSOR.get(self.service_name, 0)
            _RR_CURSOR[self.service_name] = cursor + 1

        if strategy == "round_robin":
            offset = cursor % len(instances)
            return instances[offset:] + instances[:offset]

        # local_first：本机命名空间实例优先，同命名空间内轮询；再拼其它节点
        local = [r for r in instances if r.get("instance", "") == ns] if ns else []
        remote = [r for r in instances if r not in local]
        if local:
            local = local[cursor % len(local):] + local[: cursor % len(local)]
        return local + remote

    def _registry_urls(self) -> list[str]:
        """注册表里的候选地址（按策略排序，去重）。"""
        urls: list[str] = []
        for row in self._ordered_instances():
            base = (row.get("base_url") or "").rstrip("/")
            if base and base not in urls:
                urls.append(base)
        return urls

    def _registry_url(self) -> str | None:
        """兼容旧接口：返回按 LB 策略排序后的第一个注册表实例地址。

        旧实现只查「本机命名空间 → 裸服务名」两个 key，取不到就 None；
        多实例负载均衡后改为返回候选列表首项——本机没有时自动跨节点兜底。
        """
        urls = self._registry_urls()
        return urls[0] if urls else None

    def _fallback_url(self) -> str:
        port = _FALLBACK_PORT.get(self.service_name, 38080)
        return f"http://127.0.0.1:{port}"

    def _candidates(self) -> list[str]:
        """候选地址，去重且保序：环境变量 → 注册表（按 LB 策略） → 本机回退。"""
        candidates: list[str] = []
        for url in (self._env_url(), *self._registry_urls()):
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

    # ---------- 冷却与在途统计 ----------

    def _mark_failure(self, url: str) -> None:
        """传输失败后给实例打冷却，避免连续打同一个坏实例。"""
        if _COOLDOWN_SECONDS <= 0:
            return
        inst = _instance_of(url)
        with self._lock:
            self._cooldown_until[inst] = time.time() + _COOLDOWN_SECONDS

    def _mark_success(self, url: str) -> None:
        inst = _instance_of(url)
        with self._lock:
            self._cooldown_until.pop(inst, None)

    def _enter_inflight(self, url: str) -> None:
        inst = _instance_of(url)
        with self._lock:
            self._inflight[inst] = self._inflight.get(inst, 0) + 1

    def _leave_inflight(self, url: str) -> None:
        inst = _instance_of(url)
        with self._lock:
            self._inflight[inst] = max(0, self._inflight.get(inst, 0) - 1)

    def _headers(self) -> dict:
        """认证头——每次请求重新生成令牌（TTL 60s），并透传 W3C traceparent。"""
        headers = {
            "X-Internal-Call": "true",
            "X-Service": self.caller_name,
            "X-Service-Token": generate_service_token(self.caller_name),
        }
        # 跨进程链路：把当前 span 的 traceparent 注入下游请求，Tempo 可串起调用链
        try:
            from opentelemetry.propagate import inject as _otel_inject
            _otel_inject(headers)
        except Exception:
            pass
        return headers

    # ---------- 请求 ----------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """按候选地址依次尝试；仅传输层错误切换，HTTP 错误原样抛出。

        per-request 超时（Issue #2479）：路由配置表 timeout_ms 经
        ``with_timeout()`` 线程本地覆盖逐请求下发，未覆盖时用客户端默认 10s；
        共享 httpx.Client.timeout 从不被改写（无粘滞）。
        """
        if not self._http:
            raise RuntimeError("httpx not available")

        req_timeout = getattr(self._tls, "timeout", None)
        if req_timeout is not None:
            kwargs.setdefault("timeout", req_timeout)
        candidates = self._candidates()
        last_exc: Exception | None = None
        for idx, base in enumerate(candidates):
            started = time.time()
            self._enter_inflight(base)
            try:
                resp = self._http.request(method, f"{base}{path}", headers=self._headers(), **kwargs)
                _annotate_gsb_span(**{
                    "gsb.instance": _instance_of(base),
                    "gsb.status": getattr(resp, "status_code", "unknown"),
                    "gsb.attempt": idx + 1,
                })
                if idx > 0:
                    log.info(f"GSB failover: service={self.service_name} used {base} (candidate #{idx + 1})")
                self._cached_url, self._cached_at = base, time.time()
                self._mark_success(base)
                _gsb_metrics.observe(
                    target=self.service_name,
                    caller=self.caller_name,
                    method=method,
                    status=getattr(resp, "status_code", "unknown"),
                    seconds=time.time() - started,
                    instance=_instance_of(base),
                )
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001
                _annotate_gsb_span(**{
                    "gsb.instance": _instance_of(base),
                    "gsb.status": _status_label(exc),
                    "gsb.attempt": idx + 1,
                    "gsb.error": type(exc).__name__,
                })
                _gsb_metrics.observe(
                    target=self.service_name,
                    caller=self.caller_name,
                    method=method,
                    status=_status_label(exc),
                    seconds=time.time() - started,
                    instance=_instance_of(base),
                )
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
                    self._mark_failure(base)
                    last_exc = exc
                    log.warning(
                        f"GSB transport failed: service={self.service_name} url={base} "
                        f"err={type(exc).__name__}; trying next candidate"
                    )
                    continue
                raise
            finally:
                self._leave_inflight(base)
        raise last_exc if last_exc else RuntimeError(f"GSB request failed: {self.service_name}{path}")

    @contextmanager
    def with_timeout(self, timeout_seconds: float | None):
        """线程本地 per-request 超时覆盖（Issue #2479 QA 审查修复 Required#3）。

        路由代理在派发 remote 调用时包一层：本次调用（本线程内、含其嵌套
        HTTP 请求）按 ``timeout_seconds`` 下发；离开上下文即恢复——不污染
        客户端默认超时，也不影响其它线程/后续方法的调用。
        ``None`` 等价不覆盖（维持默认 10s）。
        """
        prev = getattr(self._tls, "timeout", None)
        if timeout_seconds and timeout_seconds > 0:
            self._tls.timeout = float(timeout_seconds)
        else:
            self._tls.timeout = None
        try:
            yield
        finally:
            self._tls.timeout = prev

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET 请求。"""
        return self._request("GET", path, params=params).json()

    def post(self, path: str, json_data: dict | None = None) -> Any:
        """POST 请求。"""
        return self._request("POST", path, json=json_data).json()

    def stream_sse(self, path: str):
        """SSE 流式读取——使用首个可用候选地址。"""
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