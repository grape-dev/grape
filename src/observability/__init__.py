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
