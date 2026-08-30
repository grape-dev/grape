"""可观察性模块：Prometheus 指标注册表、HTTP 请求埋点中间件、配置。

参考 multica server/internal/metrics/（registry.go / config.go / http.go），
适配 grape 的 Python/FastAPI 技术栈。

启用方式：设置环境变量 METRICS_ADDR（如 127.0.0.1:9090）。
默认禁用——不设置则不启动 metrics server，中间件也不挂载（零开销）。
"""
import os
import time
from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST

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
            HTTP_REQUEST_DURATION.labels(**labels).observe(duration)
            raise
        finally:
            HTTP_IN_FLIGHT.dec()

        duration = time.time() - start
        status = str(response.status_code)
        route = _route_pattern(request)
        labels = {"method": request.method, "route": route, "status": status}
        HTTP_REQUESTS_TOTAL.labels(**labels).inc()
        HTTP_REQUEST_DURATION.labels(**labels).observe(duration)
        return response


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


def metrics_endpoint():
    """返回 Prometheus exposition 格式的指标数据。"""
    from fastapi.responses import Response
    return Response(
        content=generate_latest(registry),
        media_type=CONTENT_TYPE_LATEST,
    )


# ===== Metrics HTTP Server（独立端口，参考 multica metrics/server.go）=====

_metrics_httpd = None


def start_metrics_server():
    """在独立端口启动 metrics HTTP server（参考 multica metrics/server.go）。

    仅在 METRICS_ADDR 启用时启动。绑定到指定地址，与主 API 分离。
    """
    global _metrics_httpd
    if not metrics_enabled():
        return

    from http.server import HTTPServer, BaseHTTPRequestHandler

    addr = metrics_addr()
    host, _, port = addr.rpartition(":")

    class _MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                body = generate_latest(registry)
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # 静默访问日志

    _metrics_httpd = HTTPServer((host or "127.0.0.1", int(port or 9090)), _MetricsHandler)

    import threading
    t = threading.Thread(target=_metrics_httpd.serve_forever, daemon=True)
    t.start()

    from src.config import log
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
        from src.runtime.service import runtime_credential_id
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
    "MySQL Max_used_connections: max simultaneous connections since server start.",
    registry=registry,
)

DB_ABORTED_CONNECTS = Counter(
    "grape_db_aborted_connects_total",
    "MySQL Aborted_connects: failed connection attempts.",
    registry=registry,
)


def collect_mysql_metrics():
    """采集 MySQL SHOW STATUS 指标。在 /metrics scrape 时调用。

    仅 MySQL 后端时有效；SQLite 后端跳过。
    """
    try:
        from src.session_manager import _is_mysql_backend, db, db_lock
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
                    DB_THREADS_CREATED.inc(value)
                elif name == "Threads_cached":
                    DB_THREADS_CACHED.set(value)
                elif name == "Max_used_connections":
                    DB_MAX_USED_CONNECTIONS.set(value)
                elif name == "Aborted_connects":
                    DB_ABORTED_CONNECTS.inc(value)
    except Exception:
        pass  # 采集失败不影响 /metrics


# ===== BusinessSampler 采样器（参考 multica metrics/business_sampler.go）=====
# 在 /metrics scrape 时执行轻量 SQL，暴露 DB 级实时业务度量。
# 安全设计：独立查询 + LIMIT 100 + 8s TTL 缓存。

import threading as _threading

_sampler_cache: dict = {"snapshot": None, "taken_at": 0}
_sampler_lock = _threading.Lock()
_SAMPLER_CACHE_TTL = 8  # 秒
_STUCK_MINUTES = 22  # running > 22min 视为卡住（worker 默认 RUNTIME_TASK_TIMEOUT=1800s=30min，22min 能在超时前预警）


# 采样器 Gauge 描述（用 ConstMetric，避免 stale series）
from prometheus_client import Gauge as _Gauge

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
        from src.session_manager import db, db_lock
        from src.runtime.health import derive_runtime_health
        from datetime import datetime, timezone, timedelta

        now_ts = _time.time()
        stuck_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_STUCK_MINUTES)).isoformat()

        with db_lock:
            c = db.cursor()

            # task_queued by source
            queued_by_source = {}
            try:
                c.execute(
                    "SELECT COALESCE(agent_type,'unknown'), COUNT(*) FROM agent_tasks "
                    "WHERE status='queued' GROUP BY agent_type"
                )
                for r in c.fetchall():
                    queued_by_source[r[0] or "unknown"] = r[1]
            except Exception:
                pass

            # task_running by source
            running_by_source = {}
            try:
                c.execute(
                    "SELECT COALESCE(agent_type,'unknown'), COUNT(*) FROM agent_tasks "
                    "WHERE status IN ('dispatched','running') GROUP BY agent_type"
                )
                for r in c.fetchall():
                    running_by_source[r[0] or "unknown"] = r[1]
            except Exception:
                pass

            # task_stuck by source
            stuck_by_source = {}
            try:
                c.execute(
                    "SELECT COALESCE(agent_type,'unknown'), COUNT(*) FROM agent_tasks "
                    "WHERE status='running' AND started_at IS NOT NULL AND started_at < ? "
                    "GROUP BY agent_type",
                    (stuck_cutoff,),
                )
                for r in c.fetchall():
                    stuck_by_source[r[0] or "unknown"] = r[1]
            except Exception:
                pass

            # runtime online
            online_local = 0
            online_remote = 0
            try:
                c.execute("SELECT status, last_seen_at, credential_id FROM agent_runtimes")
                for r in c.fetchall():
                    h = derive_runtime_health(r[0] or "offline", r[1], now_ts)
                    if h == "online":
                        if (r[2] or 0) > 0:
                            online_remote += 1
                        else:
                            online_local += 1
            except Exception:
                pass

        # 更新 Prometheus gauges
        all_sources = set(queued_by_source) | set(running_by_source) | set(stuck_by_source)
        for src in all_sources:
            SAMPLER_TASK_QUEUED.labels(source=src).set(queued_by_source.get(src, 0))
            SAMPLER_TASK_RUNNING.labels(source=src).set(running_by_source.get(src, 0))
            SAMPLER_TASK_STUCK.labels(source=src).set(stuck_by_source.get(src, 0))

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
