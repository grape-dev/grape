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
