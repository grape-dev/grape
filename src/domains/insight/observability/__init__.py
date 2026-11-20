"""可观察性模块：Prometheus 指标注册表、HTTP 请求埋点中间件、配置。

参考 multica server/internal/metrics/（registry.go / config.go / http.go），
适配 grape 的 Python/FastAPI 技术栈。

启用方式：设置环境变量 METRICS_ADDR（如 127.0.0.1:9090）。
默认禁用——不设置则不启动 metrics server，中间件也不挂载（零开销）。
"""
import os
import time

# prometheus_client 是服务端依赖（A/B/C 都装）；worker 包按最小依赖原则不带它，
# 但 worker 侧需要通过 src.kernel.otel 报 span（架构守护 BLOCKED-1）。
# 因此这里做可选导入：缺失时提供 no-op 指标桩，保证模块可导入（worker 不采集指标）。
try:
    from prometheus_client import (Counter, Histogram, Gauge, CollectorRegistry,
                                   generate_latest, CONTENT_TYPE_LATEST)
    _PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover - worker 包路径
    _PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _NoopMetric:  # type: ignore[no-redef]
        """无 prometheus_client 时的指标桩：所有操作静默成功。"""

        def __init__(self, *a, **k):
            pass

        def labels(self, *a, **k):
            return self

        def inc(self, *a, **k):
            pass

        def dec(self, *a, **k):
            pass

        def observe(self, *a, **k):
            pass

        def set(self, *a, **k):
            pass

    Counter = Histogram = Gauge = _NoopMetric  # type: ignore[misc,assignment]

    class CollectorRegistry:  # type: ignore[no-redef]
        def __init__(self, *a, **k):
            pass

    def generate_latest(*a, **k) -> bytes:  # type: ignore[misc]
        return b""

# ===== 配置（参考 multica metrics/config.go）=====


def metrics_enabled() -> bool:
    """METRICS_ADDR 非空则启用。"""
    return bool(os.environ.get("METRICS_ADDR", "").strip())


def metrics_addr() -> str:
    return os.environ.get("METRICS_ADDR", "").strip()


# ===== 独立 Registry（不污染默认 Registry）=====

registry = CollectorRegistry()

# build_info（参考 multica multica_build_info）
_build_info = Gauge(
    "grape_build_info",
    "Build information for the grape server.",
    ["version", "commit"],
    registry=registry,
)
_build_info.labels(version="2.0.0", commit=os.environ.get("GIT_COMMIT", "unknown")).set(1)

# ===== HTTP 指标（参考 multica metrics/http.go）=====

HTTP_REQUESTS_TOTAL = Counter(
    "grape_http_requests_total",
    "Total HTTP requests served by the API server.",
    ["method", "route", "status"],
    registry=registry,
)

HTTP_REQUEST_DURATION = Histogram(
    "grape_http_request_duration_seconds",
    "HTTP request duration observed by the API server.",
    ["method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=registry,
)

# GSB 跨域调用指标：收集器定义在 kernel（kernel 不能依赖 domains），
# 这里把它们的收集器注册进本模块的 registry，/metrics 才能带出来。
from src.kernel.gsb.metrics import install as _install_gsb_metrics
from src.kernel.org_metrics import install as _install_org_metrics

_install_gsb_metrics(registry)
_install_org_metrics(registry)

HTTP_IN_FLIGHT = Gauge(
    "grape_http_in_flight_requests",
    "Current number of in-flight HTTP requests served by the API server.",
    registry=registry,
)

# 健康探针路径不计入指标（参考 multica isHealthProbePath）
_HEALTH_PATHS = frozenset({"/health", "/healthz", "/readyz", "/metrics"})


def is_health_probe_path(path: str) -> bool:
    return path in _HEALTH_PATHS


# ===== FastAPI 中间件 =====


def metrics_middleware(app):
    """挂载 HTTP 请求指标中间件到 FastAPI app。

    仅在 METRICS_ADDR 启用时挂载，否则零开销。
    """
    if not metrics_enabled():
        return

    from starlette.requests import Request
    from starlette.responses import Response

    @app.middleware("http")
    async def _record_http_metrics(request: Request, call_next):
        if is_health_probe_path(request.url.path):
            return await call_next(request)

        HTTP_IN_FLIGHT.inc()
        start = time.time()
        try:
            response = await call_next(request)
        except Exception:
            # 异常也计入 500
            duration = time.time() - start
            route = _route_pattern(request)
            labels = {"method": request.method, "route": route, "status": "500"}
            HTTP_REQUESTS_TOTAL.labels(**labels).inc()
            HTTP_REQUEST_DURATION.labels(**labels).observe(duration, exemplar=_current_exemplar())
            raise
        finally:
            HTTP_IN_FLIGHT.dec()

        duration = time.time() - start
        status = str(response.status_code)
        route = _route_pattern(request)
        labels = {"method": request.method, "route": route, "status": status}
        HTTP_REQUESTS_TOTAL.labels(**labels).inc()
        HTTP_REQUEST_DURATION.labels(**labels).observe(duration, exemplar=_current_exemplar())
        return response


def _current_exemplar() -> dict | None:
    """把当前 span 的 trace_id/span_id 挂到指标上（OpenMetrics exemplar）。

    排障价值：P95 面板上一个尖峰点下面能直接点进那条 trace，不用先按时间猜。
    没有 OTel（未启用/采样丢弃）时返回 None，指标照常导出、只是没有 exemplar。
    """
    try:
        from opentelemetry import trace as _trace
        sp = _trace.get_current_span()
        if sp is None:
            return None
        ctx = sp.get_span_context()
        if ctx is None or not ctx.is_valid:
            return None
        return {
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id": format(ctx.span_id, "016x"),
        }
    except Exception:
        return None


def _route_pattern(request) -> str:
    """提取路由模板（如 /api/dashboard/{owner}/{repo}），而非具体路径。

    FastAPI 的 request.scope["route"] 有 .path_format 属性。
    兜底用具体 path（至少能区分 endpoint）。
    """
    try:
        route = request.scope.get("route")
        if route and hasattr(route, "path_format"):
            return route.path_format
        if route and hasattr(route, "path"):
            return route.path
    except Exception:
        pass
    return "unmatched"


# ===== /metrics 端点 =====


def _openmetrics_body() -> tuple[bytes, str]:
    """按 OpenMetrics 导出（exemplar 只有这个格式带得出去），失败回退旧格式。"""
    try:
        from prometheus_client.openmetrics.exposition import (
            CONTENT_TYPE_LATEST as _OM_CT,
            generate_latest as _om_generate_latest,
        )
        return _om_generate_latest(registry), _OM_CT
    except Exception:
        return generate_latest(registry), CONTENT_TYPE_LATEST


def metrics_endpoint():
    """返回 Prometheus exposition 格式的指标数据。

    Prometheus 抓取时带 ``Accept: application/openmetrics-text;...``，这时返回
    OpenMetrics（含 exemplar → trace_id）；其它调用方仍拿 0.0.4 文本。
    """
    from fastapi.responses import Response
    on_metrics_scrape()   # DB/业务度量是"拉取时采样"，必须先刷新再导出（否则恒为 0）
    body, ctype = _openmetrics_body()
    return Response(content=body, media_type=ctype)


# ===== Metrics HTTP Server（独立端口，参考 multica metrics/server.go）=====

_metrics_httpd = None


def start_metrics_server():
    """在独立端口启动 metrics HTTP server（参考 multica metrics/server.go）。

    仅在 METRICS_ADDR 启用时启动。绑定到指定地址，与主 API 分离。
    幂等（three-process double-import fix 2026-11-04）：grape_server 与
    main.py 双重调用/模块重复加载时只启动一次；端口被占（TIME_WAIT/
    重复绑定）时降级跳过而非抛异常阻断应用启动。
    """
    global _metrics_httpd
    if _metrics_httpd is not None:
        return
    if not metrics_enabled():
        return

    from http.server import HTTPServer, BaseHTTPRequestHandler

    addr = metrics_addr()
    host, _, port = addr.rpartition(":")

    class _MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                on_metrics_scrape()   # 刷新 DB/业务采样（拉取时采样，否则恒为 0）
                want_om = "openmetrics" in (self.headers.get("Accept") or "").lower()
                body, ctype = _openmetrics_body() if want_om else (generate_latest(registry), CONTENT_TYPE_LATEST)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # 静默访问日志

    try:
        _metrics_httpd = HTTPServer((host or "127.0.0.1", int(port or 9090)), _MetricsHandler)
    except OSError as _bind_err:
        # 端口被占（快速重启 TIME_WAIT / 重复入口）：降级跳过，不阻断应用启动
        from src.kernel.config import log as _log
        _log.warning(f"metrics server skipped: bind failed on {addr} ({_bind_err})")
        _metrics_httpd = None
        return

    import threading
    t = threading.Thread(target=_metrics_httpd.serve_forever, daemon=True)
    t.start()

    from src.kernel.config import log
    log.info(f"metrics server starting on {addr}")


# ===== 业务指标（参考 multica metrics/business.go）=====
# 任务生命周期 Counter + Histogram + Gauge

_TASK_BUCKETS = (1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 3600, 7200)

TASK_ENQUEUED = Counter(
    "grape_agent_task_enqueued_total",
    "Total agent tasks enqueued.",
    ["source", "runtime_mode"],
    registry=registry,
)

TASK_DISPATCHED = Counter(
    "grape_agent_task_dispatched_total",
    "Total agent tasks dispatched to a runtime.",
    ["source", "runtime_mode"],
    registry=registry,
)

TASK_TERMINAL = Counter(
    "grape_agent_task_terminal_total",
    "Total agent tasks that reached a terminal state.",
    ["source", "runtime_mode", "terminal_status"],
    registry=registry,
)

TASK_FAILED = Counter(
    "grape_agent_task_failed_total",
    "Total failed agent tasks by failure reason.",
    ["source", "runtime_mode", "failure_reason"],
    registry=registry,
)

TASK_QUEUE_WAIT = Histogram(
    "grape_agent_task_queue_wait_seconds",
    "Time agent tasks spent queued before dispatch.",
    ["source", "runtime_mode"],
    buckets=_TASK_BUCKETS,
    registry=registry,
)

TASK_RUN_SECONDS = Histogram(
    "grape_agent_task_run_seconds",
    "Time agent tasks spent running before a terminal state.",
    ["source", "runtime_mode", "terminal_status"],
    buckets=_TASK_BUCKETS,
    registry=registry,
)

TASK_TOTAL_SECONDS = Histogram(
    "grape_agent_task_total_seconds",
    "Total time from agent task creation to terminal state.",
    ["source", "runtime_mode", "terminal_status"],
    buckets=_TASK_BUCKETS,
    registry=registry,
)

TASK_IN_PROGRESS = Gauge(
    "grape_agent_task_in_progress",
    "Current agent tasks dispatched by this process and not yet terminal.",
    ["source", "runtime_mode"],
    registry=registry,
)


# ===== 标签规范化（参考 multica metrics/labels.go）=====

_VALID_SOURCES = frozenset({"chat", "issue", "webhook", "manual", "api", "vod", "autopilot", "other"})
_VALID_MODES = frozenset({"local", "remote", "unknown"})
_VALID_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def _normalize_source(source: str) -> str:
    s = (source or "").strip().lower()
    return s if s in _VALID_SOURCES else "other"


def _normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    return m if m in _VALID_MODES else "unknown"


def _normalize_terminal(status: str) -> str:
    s = (status or "").strip().lower()
    return s if s in _VALID_TERMINAL else "unknown"


def _runtime_mode(runtime_id: int) -> str:
    """从 runtime_id 推导 runtime_mode（local/remote）。"""
    try:
        from src.domains.runtime.public import runtime_credential_id
        cid = runtime_credential_id(runtime_id)
        if cid is None:
            return "unknown"
        return "remote" if cid > 0 else "local"
    except Exception:
        return "unknown"


# ===== 埋点函数（业务代码调用这些，不直接碰 Prometheus 对象）=====

# 运行中任务跟踪（用于 in_progress gauge 增减）
_in_progress_tracker: dict[str, dict] = {}


def record_task_enqueued(task_id: str, source: str, runtime_id: int):
    """任务入队时调用。"""
    s = _normalize_source(source)
    m = _normalize_mode(_runtime_mode(runtime_id))
    TASK_ENQUEUED.labels(source=s, runtime_mode=m).inc()
    _in_progress_tracker[task_id] = {"source": s, "runtime_mode": m}


def record_task_dispatched(task_id: str, source: str, runtime_id: int, queue_wait_seconds: float = -1):
    """任务被 claim/分发时调用。"""
    s = _normalize_source(source)
    m = _normalize_mode(_runtime_mode(runtime_id))
    TASK_DISPATCHED.labels(source=s, runtime_mode=m).inc()
    if queue_wait_seconds >= 0:
        TASK_QUEUE_WAIT.labels(source=s, runtime_mode=m).observe(queue_wait_seconds)
    _in_progress_tracker[task_id] = {"source": s, "runtime_mode": m}


def record_task_terminal(task_id: str, source: str, runtime_id: int,
                         terminal_status: str, run_seconds: float = -1, total_seconds: float = -1):
    """任务到达终态时调用。"""
    s = _normalize_source(source)
    m = _normalize_mode(_runtime_mode(runtime_id))
    ts = _normalize_terminal(terminal_status)
    TASK_TERMINAL.labels(source=s, runtime_mode=m, terminal_status=ts).inc()
    if run_seconds >= 0:
        TASK_RUN_SECONDS.labels(source=s, runtime_mode=m, terminal_status=ts).observe(run_seconds)
    if total_seconds >= 0:
        TASK_TOTAL_SECONDS.labels(source=s, runtime_mode=m, terminal_status=ts).observe(total_seconds)
    _in_progress_tracker.pop(task_id, None)


def record_task_failed(task_id: str, source: str, runtime_id: int, failure_reason: str = ""):
    """任务失败时调用。"""
    s = _normalize_source(source)
    m = _normalize_mode(_runtime_mode(runtime_id))
    TASK_FAILED.labels(source=s, runtime_mode=m, failure_reason=failure_reason or "unknown").inc()


# ===== LLM token 用量指标（参考 multica metrics/business.go llmTokens）=====

LLM_TOKENS = Counter(
    "grape_llm_tokens_total",
    "Total LLM tokens by provider, model, and token type.",
    ["provider", "model", "token_type"],
    registry=registry,
)

LLM_REQUESTS = Counter(
    "grape_llm_request_total",
    "Total LLM usage reports by provider and model.",
    ["provider", "model"],
    registry=registry,
)


def record_llm_usage(provider: str, model: str, input_tokens: int, output_tokens: int, cache_tokens: int = 0):
    """记录 LLM token 用量。在 /tasks/{task_id}/usage 端点调用。

    参考 multica metrics/business.go RecordLLMUsage。
    """
    p = (provider or "unknown").strip().lower() or "unknown"
    m = (model or "unknown").strip().lower() or "unknown"
    if input_tokens > 0:
        LLM_TOKENS.labels(provider=p, model=m, token_type="input").inc(input_tokens)
    if output_tokens > 0:
        LLM_TOKENS.labels(provider=p, model=m, token_type="output").inc(output_tokens)
    if cache_tokens > 0:
        LLM_TOKENS.labels(provider=p, model=m, token_type="cache").inc(cache_tokens)
    LLM_REQUESTS.labels(provider=p, model=m).inc()


# ===== MySQL 连接池指标（参考 multica metrics/db.go，适配 grape MySQL）=====

DB_THREADS_CONNECTED = Gauge(
    "grape_db_threads_connected",
    "MySQL Threads_connected: current connected threads.",
    registry=registry,
)

DB_THREADS_RUNNING = Gauge(
    "grape_db_threads_running",
    "MySQL Threads_running: active threads executing queries.",
    registry=registry,
)

DB_THREADS_CREATED = Counter(
    "grape_db_threads_created_total",
    "MySQL Threads_created: total threads created since server start.",
    registry=registry,
)

DB_THREADS_CACHED = Gauge(
    "grape_db_threads_cached",
    "MySQL Threads_cached: threads in the thread cache.",
    registry=registry,
)

DB_MAX_USED_CONNECTIONS = Gauge(
    "grape_db_max_used_connections",
    "MySQL Max_used_connections: HISTORICAL PEAK of simultaneous connections since server "
    "start. NOT the configured limit — do not use it as an alert denominator (see "
    "grape_db_max_connections).",
    registry=registry,
)

DB_MAX_CONNECTIONS = Gauge(
    "grape_db_max_connections",
    "MySQL max_connections: the configured connection ceiling (SHOW VARIABLES). "
    "Use this as the denominator for pool-usage alerts.",
    registry=registry,
)

DB_ABORTED_CONNECTS = Counter(
    "grape_db_aborted_connects_total",
    "MySQL Aborted_connects: failed connection attempts.",
    registry=registry,
)

# SHOW STATUS 里这些值是**服务端累计值**（单调递增），不是"本次采樣的增量"。
# 直接用 .inc(value) 会按照「累计值 × 抓取次数」放大：实测 Aborted_connects=12 被报成 48/min、
# Threads_created 被报成 408/min，让基于 increase() 的告警长期误报。
# 这里记住上次观测到的累计值，只累加真正的增量。
_COUNTER_LAST: dict = {}


def _inc_by_delta(counter, name: str, value: int) -> None:
    """把服务端累计值转成 Prometheus 计数器增量（首次观测按已累计量补齐）。"""
    prev = _COUNTER_LAST.get(name)
    if prev is None or value < prev:  # 首次观测 / 服务端重启导致计数回绕
        counter.inc(value)
    else:
        counter.inc(value - prev)
    _COUNTER_LAST[name] = value


def collect_mysql_metrics():
    """采集 MySQL SHOW STATUS 指标。在 /metrics scrape 时调用。

    仅 MySQL 后端时有效；SQLite 后端跳过。
    """
    try:
        from src.kernel.session_manager import _is_mysql_backend
        from src.kernel.owner_db import db_pair
        db, db_lock = db_pair("insight")
        if not _is_mysql_backend():
            return
        with db_lock:
            c = db.cursor()
            c.execute("SHOW STATUS WHERE Variable_name IN "
                      "('Threads_connected','Threads_running','Threads_created',"
                      "'Threads_cached','Max_used_connections','Aborted_connects')")
            for row in c.fetchall():
                name, value = row[0], int(row[1])
                if name == "Threads_connected":
                    DB_THREADS_CONNECTED.set(value)
                elif name == "Threads_running":
                    DB_THREADS_RUNNING.set(value)
                elif name == "Threads_created":
                    _inc_by_delta(DB_THREADS_CREATED, name, value)
                elif name == "Threads_cached":
                    DB_THREADS_CACHED.set(value)
                elif name == "Max_used_connections":
                    DB_MAX_USED_CONNECTIONS.set(value)
                elif name == "Aborted_connects":
                    _inc_by_delta(DB_ABORTED_CONNECTS, name, value)
            # 配置上限（分母）：SHOW VARIABLES，不是 STATUS —— 别和 Max_used_connections（历史峰值）混用
            try:
                c.execute("SHOW VARIABLES WHERE Variable_name='max_connections'")
                _r = c.fetchone()
                if _r and _r[1] is not None:
                    DB_MAX_CONNECTIONS.set(int(_r[1]))
            except Exception:
                pass
    except Exception:
        pass  # 采集失败不影响 /metrics


# ===== BusinessSampler 采样器（参考 multica metrics/business_sampler.go）=====
# 在 /metrics scrape 时执行轻量 SQL，暴露 DB 级实时业务度量。
# 安全设计：独立查询 + LIMIT 100 + 8s TTL 缓存。

import threading as _threading

_sampler_cache: dict = {"snapshot": None, "taken_at": 0}
_sampler_lock = _threading.Lock()
_SAMPLER_CACHE_TTL = 8  # 秒


def _stuck_minutes() -> int:
    """stuck 预警阈值（分钟，Issue #470：随统一超时配置派生，不再写死 22）。

    采样器 8s TTL，每轮取当前配置值——管理台/配置文件修改后 ≤8s 生效。
    """
    from src.kernel.config import get_stuck_threshold_minutes
    return get_stuck_threshold_minutes()


# 采样器 Gauge 描述（用 ConstMetric，避免 stale series）。
# worker 包不带 prometheus_client：这里复用上面的可选导入（缺失时 _Gauge 为 no-op 桩）。
_Gauge = Gauge

SAMPLER_TASK_QUEUED = _Gauge(
    "grape_task_queued",
    "Current agent_tasks rows in queued status. Sampled from DB.",
    ["source"],
    registry=registry,
)

SAMPLER_TASK_RUNNING = _Gauge(
    "grape_task_running",
    "Current agent_tasks rows in dispatched/running status. Sampled from DB.",
    ["source"],
    registry=registry,
)

SAMPLER_TASK_STUCK = _Gauge(
    "grape_task_stuck",
    "Current running agent_tasks with started_at older than 30 minutes. Sampled from DB.",
    ["source"],
    registry=registry,
)

SAMPLER_RUNTIME_ONLINE = _Gauge(
    "grape_runtime_online",
    "Count of agent_runtimes with last_seen_at within heartbeat window. Sampled from DB.",
    ["mode"],
    registry=registry,
)

SAMPLER_IN_PROGRESS = _Gauge(
    "grape_task_in_progress",
    "Current agent_tasks dispatched+running. Sampled from DB.",
    registry=registry,
)


_known_sources: set[str] = set()


def _apply_source_gauges(queued: dict, running: dict, stuck: dict) -> set[str]:
    """把三类任务 gauge 刷到 Prometheus；本轮消失的 source 必须显式归零。

    旧写法只遍历"本轮查到的 source"：某类任务全部结束后集合为空，
    gauge 会永远保留上一次的值（实测「队列/执行/卡住」面板长期显示 running=1，
    而库里已经没有 running 任务）。这里按历史出现过的 source 全量刷新。
    source 取值是 agent_type（vod/issue/coding/qa/sre…），基数有限。
    """
    _known_sources.update(set(queued) | set(running) | set(stuck))
    for src in _known_sources:
        SAMPLER_TASK_QUEUED.labels(source=src).set(queued.get(src, 0))
        SAMPLER_TASK_RUNNING.labels(source=src).set(running.get(src, 0))
        SAMPLER_TASK_STUCK.labels(source=src).set(stuck.get(src, 0))
    return set(_known_sources)


def collect_business_sampler():
    """BusinessSampler：scrape 时执行 SQL 采样，带 TTL 缓存。

    参考 multica BusinessSamplerCollector.Collect()。
    """
    import time as _time

    now = _time.time()
    with _sampler_lock:
        snap = _sampler_cache["snapshot"]
        if snap is not None and now - _sampler_cache["taken_at"] < _SAMPLER_CACHE_TTL:
            return  # 缓存未过期，跳过

    # 采集新快照
    try:
        from src.domains.runtime import public as runtime_public
        from src.domains.runtime.public import derive_runtime_health
        from datetime import datetime, timezone, timedelta

        now_ts = _time.time()
        stuck_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_stuck_minutes())).isoformat()

        # 四条 SQL（agent_tasks ×3 + agent_runtimes ×1）全归 runtime 域：
        # 一次调用取回同一快照，连接/锁/方言都由 owner 原语自理，本模块不再持游标。
        snap = runtime_public.task_sampler_snapshot_any(stuck_cutoff)
        queued_by_source = snap["queued"]
        running_by_source = snap["running"]
        stuck_by_source = snap["stuck"]

        # runtime online：在线/离线判定属本域分析口径，owner 只给原始列
        online_local = 0
        online_remote = 0
        for r in snap["runtimes"]:
            h = derive_runtime_health(r[0] or "offline", r[1], now_ts)
            if h == "online":
                if (r[2] or 0) > 0:
                    online_remote += 1
                else:
                    online_local += 1

        # 更新 Prometheus gauges（消失的 source 会显式归零，见 _apply_source_gauges）
        _apply_source_gauges(queued_by_source, running_by_source, stuck_by_source)

        SAMPLER_RUNTIME_ONLINE.labels(mode="local").set(online_local)
        SAMPLER_RUNTIME_ONLINE.labels(mode="remote").set(online_remote)

        in_progress = sum(running_by_source.values())
        SAMPLER_IN_PROGRESS.set(in_progress)

        with _sampler_lock:
            _sampler_cache["snapshot"] = {
                "queued": queued_by_source,
                "running": running_by_source,
                "stuck": stuck_by_source,
                "online_local": online_local,
                "online_remote": online_remote,
                "in_progress": in_progress,
            }
            _sampler_cache["taken_at"] = now
    except Exception:
        pass  # 采样失败不影响 /metrics


def on_metrics_scrape():
    """在 /metrics 端点被 scrape 时调用，触发 MySQL 指标采集 + BusinessSampler。

    参考 multica /metrics handler — multica 用 prometheus.Collector 自动采集，
    grape 用手动触发方式（更简单，适配 prometheus_client Python SDK）。
    """
    collect_mysql_metrics()
    collect_business_sampler()
