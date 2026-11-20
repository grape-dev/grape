"""可观察性 · OTel 基座（LGTM Traces，2026-11-05 MR-1 Step 1）。

单一初始化源：A/B/C/webhook 四入口 import 后各自获得独立 service.name 的
TracerProvider + OTLP gRPC exporter + FastAPI 自动 HTTP span。

设计要点（docs/weekly-10/full-observability-tracing-plan.md）：
- OTEL_EXPORTER_OTLP_ENDPOINT（grape.env）非空才启用；缺省禁用零开销
- 采样现状（架构守护 Medium-3）：head-based ParentBased(TraceIdRatioBased)，
  默认比例 OTEL_SAMPLER_RATIO=0.1；错误/慢请求「全采」尚未实现——需在
  Alloy/collector 侧接入 tail_sampling 后才生效，当前文档不再作该项承诺
- 失败不致命：全链 try/except，Tempo 断网/超时对业务零影响（exporter 异步）
- service.name 由调用方传入（grape-server-a/b/c、grape-webhook、grape-worker），
  resource 统一补 host/env/version

与平台既有确定性 trace id 体系（src/domains/insight/observability/trace.py，tr_<sha1(task_id)>）
的关系：MR-1 仅 HTTP span 与日志注入（OTel 自动生成 trace id）；MR-2 把任务
生命周期接入，task_id/pipeline_trace_id 作为 span 属性与业务 id 同源对齐。
"""
import os
import platform
import re
from contextlib import contextmanager


def _hostname() -> str:
    """主机名（跨平台）。

    `os.uname()` 仅 Unix；Windows worker 上会抛 `AttributeError: module 'os' has
    no attribute 'uname'`，把 OTel 初始化整段打成 "OTel init failed" 日志（2026-11-19
    实测于 Windows runtime-agent，service=grape-task）。platform.node() 全平台可用。
    """
    return (os.environ.get("HOSTNAME")
            or (os.uname().nodename if hasattr(os, "uname") else "")
            or platform.node()
            or "unknown")

_inited_service = None
_inited_provider = None


def otel_enabled() -> bool:
    """OTel 启用条件：显式 gRPC 端点（主控/CLI）或 worker 中继模式（MR-2b）。"""
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return True
    return _worker_relay_exporter_kwargs() is not None


def start_external_span(kind: str, target: str, attrs: dict | None = None):
    """手动生命周期的外部 span（生成器内不能用 with）——调用方负责 .end()。"""
    if not otel_enabled():
        return None
    from opentelemetry import trace
    span = trace.get_tracer(__name__).start_span(
        "%s %s" % (kind, target), kind=trace.SpanKind.CLIENT)
    # 注意：start_span 不自动入上下文——子 span（后续的外部调用）不会自动挂到它下面；
    # 对引擎执行 span 可接受（其子级价值低），换取生成器内手动生命周期的简单性。
    span.set_attribute("dep.kind", kind)
    span.set_attribute("dep.target", str(target)[:200])
    for k, v in (attrs or {}).items():
        try:
            span.set_attribute(str(k)[:60], str(v)[:200])
        except Exception:
            pass
    return span


@contextmanager
def feature_span(name: str, attrs: dict | None = None):
    """业务特性 span（A13：新增特性接入 Tempo 的最小切口）。

    - name: span 名（如 ``org.provision`` / ``org.dispatch.create_child``）
    - attrs: 业务属性（org_id/repo_key/action 等，不含凭据）
    - OTel 未启用时零开销空上下文；异常记录 error.type/error.message。
    """
    if not otel_enabled():
        yield None
        return
    from opentelemetry import trace
    tracer = trace.get_tracer("grape.feature")
    with tracer.start_as_current_span(name) as span:
        for k, v in (attrs or {}).items():
            try:
                span.set_attribute(str(k)[:60], str(v)[:200])
            except Exception:
                pass
        try:
            yield span
        except Exception as e:
            span.set_attribute("error.type", type(e).__name__)
            span.set_attribute("error.message", str(e)[:200])
            span.record_exception(e)
            raise


def annotate_current_span(**attrs: object) -> None:
    """给当前 span 追加属性（未启用 / 无活动 span 时零开销）。

    总线客户端在真正发 HTTP 的那一层补 instance/status：dispatch 层只知道
    service/method，解析出来的目标实例只有 client 拿得到。
    """
    if not otel_enabled():
        return
    try:
        from opentelemetry import trace as _trace
        span = _trace.get_current_span()
        if span is None or not span.is_recording():
            return
        for key, value in attrs.items():
            if value is None:
                continue
            span.set_attribute(str(key)[:60], str(value)[:200])
    except Exception:  # noqa: BLE001 — 埋点失败不影响业务
        return


# 出站 span 命名：requests 自动插桩默认给 "GET"/"POST"，既看不出打的是谁，
# 也没法按 URL 归并（P95 面板里全挤在一条 "GET" 上）。这里把 span 名改成
# "METHOD /masked/path"，id 段（uuid / 长 hex / 纯数字）归一成 {id} 防基数爆炸。
_ID_SEGMENT_RE = re.compile(
    r"(?<=/)(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{16,}|\d+)(?=/|$)"
)


def _mask_url_path(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        path = urlsplit(url or "").path or ""
        return _ID_SEGMENT_RE.sub("{id}", path)[:120]
    except Exception:  # noqa: BLE001
        return ""


_HTTP_METHOD_BYTES = (bytes, bytearray)


def _method_of(request) -> str:
    """httpx 的 Request.method 在某些版本/路径上是 bytes，直接拼进 span 名会得到 b'GET'。"""
    method = getattr(request, "method", "HTTP")
    if isinstance(method, _HTTP_METHOD_BYTES):
        return method.decode("ascii", "ignore") or "HTTP"
    return str(method)


def _requests_request_hook(span, request) -> None:
    """requests 出站：span 名 + 对端 + 超时，不用再点进 attributes 才能认。"""
    if span is None or not span.is_recording():
        return
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(getattr(request, "url", "") or "")
        path = _mask_url_path(getattr(request, "url", ""))
        span.update_name(f"{_method_of(request)} {path or '/'}")
        if parts.hostname:
            span.set_attribute("http.peer", parts.hostname)
            span.set_attribute("server.address", parts.hostname)
        if parts.port:
            span.set_attribute("server.port", parts.port)
    except Exception:  # noqa: BLE001
        return


def _requests_response_hook(span, request, response) -> None:
    """requests 出站：状态码 / 重试次数 / 5xx 标 ERROR。"""
    if span is None or not span.is_recording():
        return
    try:
        status = getattr(response, "status_code", None)
        if status is not None:
            span.set_attribute("http.status_code", status)
        elapsed = getattr(response, "elapsed", None)
        if elapsed is not None:
            span.set_attribute("http.elapsed_ms", int(elapsed.total_seconds() * 1000))
        history = getattr(getattr(getattr(response, "raw", None), "retries", None),
                          "history", None)
        if history:
            span.set_attribute("http.retries", len(history))
        if status is not None and status >= 500:
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
    except Exception:  # noqa: BLE001
        return


_HTTPX_INSTRUMENTED = False


def _httpx_request_hook(span, request) -> None:
    """httpx 出站：与 requests 同款命名（多数客户端走 httpx，requests 只覆盖旧代码）。"""
    if span is None or not span.is_recording():
        return
    try:
        url = str(getattr(request, "url", "") or "")
        span.update_name(f"{_method_of(request)} {_mask_url_path(url) or '/'}")
        host = getattr(getattr(request, "url", None), "host", None)
        if host:
            span.set_attribute("http.peer", host)
            span.set_attribute("server.address", host)
        port = getattr(getattr(request, "url", None), "port", None)
        if port:
            span.set_attribute("server.port", port)
    except Exception:  # noqa: BLE001
        return


def _httpx_response_hook(span, request, response) -> None:
    if span is None or not span.is_recording():
        return
    try:
        status = getattr(response, "status_code", None)
        if status is not None:
            span.set_attribute("http.status_code", status)
        ext = getattr(response, "extensions", None) or {}
        elapsed = ext.get("elapsed") if isinstance(ext, dict) else None
        if elapsed is not None:
            span.set_attribute("http.elapsed_ms", int(elapsed.total_seconds() * 1000))
        if status is not None and status >= 500:
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
    except Exception:  # noqa: BLE001
        return


def _install_httpx_instrumentor() -> None:
    """httpx 客户端插桩（可选依赖：装了就启用，没装不影响其它埋点）。

    2026-11-20 实测：beta 的出站调用绝大多数走 httpx（GSB 总线、LLM SDK），
    只装 requests 插桩等于出站全黑——trace 里从 server span 直接跳到 callee。
    """
    global _HTTPX_INSTRUMENTED
    if _HTTPX_INSTRUMENTED:
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument(
            request_hook=_httpx_request_hook,
            response_hook=_httpx_response_hook,
        )
        _HTTPX_INSTRUMENTED = True
    except Exception:  # noqa: BLE001
        return


@contextmanager
def external_call_span(kind: str, target: str, attrs: dict | None = None):
    """外部依赖调用 span（MR-3，2026-11-05）：GitCode/GitHub/OBS/引擎进程等黑盒展开。

    - kind: 依赖类别（vcs / obs / engine / http ...）
    - target: 具体目标（API path、对象 key、backend 名）——不含 token/密钥
    - attrs: 额外业务属性（provider、repo_key、size 等）
    - OTel 未启用时是零开销空上下文；异常自动记录（error.type/error.message）。
    """
    if not otel_enabled():
        yield None
        return
    from opentelemetry import trace
    tracer = trace.get_tracer(__name__)
    name = "%s %s" % (kind, target)
    with tracer.start_as_current_span(name, kind=trace.SpanKind.CLIENT) as span:
        try:
            span.set_attribute("dep.kind", kind)
            span.set_attribute("dep.target", str(target)[:200])
            for k, v in (attrs or {}).items():
                try:
                    span.set_attribute(str(k)[:60], str(v)[:200])
                except Exception:
                    pass
            yield span
        except Exception as e:
            span.set_attribute("error.type", type(e).__name__)
            span.set_attribute("error.message", str(e)[:200])
            span.record_exception(e)
            raise


def _worker_relay_exporter_kwargs():
    """MR-2b：worker 进程特征 = RUNTIME_TOKEN 非空。

    返回 OTLP/HTTP exporter 配置（指向主控中继 /api/runtime/traces + Bearer token）；
    非 worker 进程（主控/CLI，无 RUNTIME_TOKEN）→ None（走 gRPC 直连分支）。
    worker 无需配置 OTEL_EXPORTER_OTLP_ENDPOINT（中继地址由 RUNTIME_SERVER_URL 推导）。
    """
    try:
        token = os.environ.get("RUNTIME_TOKEN", "").strip()
        server = os.environ.get("RUNTIME_SERVER_URL", "").strip()
        if not (token and server):
            return None
        return {
            "endpoint": server.rstrip("/") + "/api/runtime/traces",
            "headers": {"Authorization": "Bearer " + token},
            "timeout": 5,
        }
    except Exception:
        return None


def init_otel(service_name: str):
    """初始化本进程的 OTel。进程级幂等：本进程只要成功初始化过一次，后续任何
    service_name 的重复调用都直接复用同一个 Provider（架构审查 P2-4）。

    早期实现以 service_name 作为幂等键，导致同一进程内 A 入口初始化
    grape-server-a 后，start_task_span 内部再调 init_otel("grape-task") 不命中，
    会重建 Provider 并二次 set_tracer_provider（SDK 拒绝，产生空转
    BatchSpanProcessor + warning）。service.name 仍由首次初始化确定。

    返回 TracerProvider（成功）/ None（未启用或初始化失败——调用方跳过埋点）。
    """
    global _inited_service, _inited_provider
    try:
        if not otel_enabled():
            return None
        if _inited_provider is not None:
            return _inited_provider

        from opentelemetry import trace, metrics
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased, ParentBased

        resource = Resource.create({
            "service.name": service_name,
            "service.version": os.environ.get("GIT_COMMIT", "dev"),
            "host": _hostname(),
            "env": os.environ.get("GRAPE_ENV", "dev"),
        })

        # 尾部采样：父上下文优先透传（跨进程同一 trace）；无父时按比例
        ratio = float(os.environ.get("OTEL_SAMPLER_RATIO", "0.1") or "0.1")
        sampler = ParentBased(TraceIdRatioBased(ratio))

        provider = TracerProvider(resource=resource, sampler=sampler)
        # MR-2b（2026-11-05）：worker 进程 → trace 走主控中继（OTLP/HTTP + worker token），
        # 不直连 LGTM（生产 LGTM 机器不对 worker 开放）。主控/CLI 维持 gRPC 直连。
        relay = _worker_relay_exporter_kwargs()
        if relay is not None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as _HTTPExporter
            exporter = _HTTPExporter(**relay)
        else:
            # MR-3（2026-11-05）：gRPC→HTTP exporter 切换。实测 Tempo 3.1.1 的 4317 gRPC
            # 接收静默丢弃 span（force_flush 成功但 Tempo 无数据），4318 HTTP 正常落库。
            # endpoint 约定不变（用户配置 :4317 也自动改写为 :4318）。
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            _ep = os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"].strip().rstrip("/")
            if _ep.endswith(":4317"):
                _ep = _ep[:-5] + ":4318"
            exporter = OTLPSpanExporter(endpoint=_ep + "/v1/traces", timeout=5)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        _inited_service = service_name
        _inited_provider = provider
        return provider
    except Exception as e:
        try:
            from src.kernel.config import log
            log.warning(f"OTel init failed (service={service_name}, tracing disabled): {e}")
        except Exception:
            pass
        return None


_REQUESTS_INSTRUMENTED = False


def instrument_fastapi(app, service_name: str):
    """FastAPI 自动 HTTP span（A/B/C/webhook 四入口通用）。幂等、失败不致命。

    service.name 优先取 GRAPE_TRACE_SERVICE——betamulti-process 下四个入口都走
    server_common.build_app，靠这个变量把 mgmt/event/worker/insight 分开，否则
    只能拿到调用方硬编码的名字（曾出现四进程同叫 grape-server-a）。

    app 上已经挂过 FastAPIInstrumentor 时直接返回：grape_server.py 在 build_app
    之后又显式调用一次，没有这道闸门会产生重复 span。
    """
    try:
        if not otel_enabled():
            return False
        if getattr(app, "_is_instrumented_by_opentelemetry", False):
            return True
        _svc = os.environ.get("GRAPE_TRACE_SERVICE", "").strip() or service_name
        init_otel(_svc)
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        # 剔除探针/指标路径，避免无意义 span 噪声（与 metrics 中间件同款口径）
        # 噪声端点剔除（2026-11-05）：worker 每 15s 的 claim/heartbeat/tags 刷屏 Tempo
        # 列表，把真正要看的任务 trace 挤出搜索窗口。任务生命周期另有专门 span
        # （task.*），协议面已在 metrics（/metrics）观测，trace 层保留完整请求以外的价值。
        # 2026-11-20 实测修正：excluded_urls 是正则、按完整 path 匹配，原来写的
        # "runtimes/.../tasks/claim"、"/runtime/heartbeat" 少了 api/ 前缀，一条都没命中，
        # 结果 worker 的 claim/heartbeat 反而被全量埋点（legacy 路径本来排除 api/ping）。
        FastAPIInstrumentor.instrument_app(app, excluded_urls=(
            "health,healthz,readyz,metrics,api/ping,"
            "api/runtime/heartbeat,api/runtime/register,api/runtime/install,"
            # 实测两条路由都吃：/api/runtime/runtimes/uid/<engine_uid>/tasks/claim
            # 和 /api/runtime/runtimes/<runtime_id>/tasks/claim-by-tag/<tag>，
            # 所以用 .* 放宽，别按固定段写（写死 uid/ 那么 claim-by-tag 会漏）。
            "api/runtime/runtimes/.*tasks/claim,"
            "api/runtime/runtimes/.*/tags,"
            "faro/collect"
        ))
        # DB span 采用「单一来源」策略（架构审查 P2-2）：只保留 mysql_compat.py 的
        # 手动埋点（start_db_span/end_db_span，带 db.operation/rowcount 且显式挂父上下文），
        # 不启用 opentelemetry-instrumentation-pymysql 自动插桩。原因：
        #   1) 两套并存时同一 SQL 会产生两个兄弟 db.query span（start_span 不入 ctx）；
        #   2) 自动插桩按 connection patch，对池化复用连接的语义不透明；
        #   3) 少一个依赖，requirements.txt 无需声明 instrumentation-pymysql。
        # 唯一例外：检测到自动插桩已生效（外部按需安装）时给出告警，避免无意双轨。
        try:
            from opentelemetry.instrumentation.pymysql import PyMySQLInstrumentor  # noqa: F401
            from src.kernel.config import log as _log
            _log.warning("PyMySQL auto-instrumentation detected while manual DB spans are active "
                         "— duplicate db.query spans possible; disable one of them")
        except Exception:
            pass
        # requests 出站 span：旧路径在 setup_tracing 里装了，新路径原来没有，
        # 切过来会让 LLM（requests 直连）与 VCS/SCM 的调用从 trace 里消失。
        global _REQUESTS_INSTRUMENTED
        if not _REQUESTS_INSTRUMENTED:
            try:
                from opentelemetry.instrumentation.requests import RequestsInstrumentor
                RequestsInstrumentor().instrument(
                    request_hook=_requests_request_hook,
                    response_hook=_requests_response_hook,
                )
                _REQUESTS_INSTRUMENTED = True
            except Exception:
                pass
        _install_httpx_instrumentor()
        return True
    except Exception as e:
        try:
            from src.kernel.config import log
            log.warning(f"FastAPI OTel instrument failed ({service_name}): {e}")
        except Exception:
            pass
        return False


_SQL_STR_RE = re.compile(r"'(?:[^'\\]|\\.)*'")
_SQL_DQ_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_SQL_IN_RE = re.compile(r"\bIN\s*\([^)]{0,400}\)", re.IGNORECASE)
_SQL_CMP_NUM_RE = re.compile(r"([=<>!]=?|<>)\s*(\d+(?:\.\d+)?)")
_SQL_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+`?([A-Za-z0-9_$.`]+)`?", re.IGNORECASE)
_SQL_OP_RE = re.compile(r"\s*([A-Za-z]+)")


def sanitize_sql(sql: str, limit: int = 512) -> str:
    """把 SQL 里的「值」抹成占位符，只留形状。

    目的：db.statement 会进 Tempo，原文里的邮箱 / token / 手机号不能再落第三处
    （日志、DB、trace 已经三份）。只做三件事——字符串字面量、IN 列表、比较运算符
    后面的数字；表名与列名保留，否则 span 就没有诊断价值了。
    """
    try:
        s = _SQL_STR_RE.sub("?", sql)
        s = _SQL_DQ_RE.sub('"?"', s)
        s = _SQL_IN_RE.sub("IN (?)", s)
        s = _SQL_CMP_NUM_RE.sub(r"\1 ?", s)
        return s[:limit]
    except Exception:
        return "?"


def _sql_table(sql: str) -> str:
    """粗提取主表名（去掉库前缀），只用于 db.sql.table 属性。"""
    try:
        m = _SQL_TABLE_RE.search(sql)
        return (m.group(1).replace("`", "").split(".")[-1] if m else "")[:64]
    except Exception:
        return ""


def start_db_span(sql: str, db_name: str = ""):
    """SQL 执行 span（DB 阶段拆解 2026-11-05）：在 execute 调用点创建，自动挂当前
    请求 trace（contextvars 传播）。OTel 未启用/无有效父 span → None（零开销）。

    架构审查 P2-3：无父上下文时（后台线程/启动期 SQL）不建 span——否则每条 SQL
    都会成为独立 trace 的根，按采样率灌进 Tempo 产生大量孤儿 trace。

    2026-11-20 补全排障必需属性：db.system / db.name（三库分得开）/ db.sql.table /
    db.operation（完整首词，原来只取 6 个字符，INSERT 会被截成 INSERT）；同时把
    statement 脱敏——原来是原文截断，邮箱与 token 直接进 Tempo。
    """
    if not otel_enabled():
        return None
    try:
        from opentelemetry import trace as _trace
        # 无有效父上下文 → 不建（避免孤儿 trace）
        _parent = _trace.get_current_span()
        if _parent is None or not _parent.get_span_context().is_valid:
            return None
        _span = _trace.get_tracer(__name__).start_span("db.query", kind=_trace.SpanKind.CLIENT)
        if not _span.get_span_context().is_valid:
            return None
        _m = _SQL_OP_RE.match(sql)
        _op = (_m.group(1) if _m else "").upper()
        _span.set_attribute("db.system", "mysql")
        _span.set_attribute("db.operation", _op)
        _span.set_attribute("db.statement", sanitize_sql(sql))
        if db_name:
            _span.set_attribute("db.name", str(db_name)[:64])
        _tbl = _sql_table(sql)
        if _tbl:
            _span.set_attribute("db.sql.table", _tbl)
        return _span
    except Exception:
        return None


def end_db_span(span, rowcount=None, exc=None):
    """结束 start_db_span 的 span；失败标记 error（status + errno + 异常事件）。

    原来只在 finally 里 end()，execute 抛异常时 span 不带任何错误标记，排障时
    看起来像「成功的一跳」——这是 2026-11-20 修掉的坑。
    """
    try:
        if span is None:
            return
        if exc is not None:
            try:
                span.set_attribute("db.error", type(exc).__name__)
                _errno = getattr(exc, "args", (None,))[0]
                if isinstance(_errno, int):
                    span.set_attribute("db.mysql_errno", _errno)
            except Exception:
                pass
            try:
                span.record_exception(exc)
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            except Exception:
                pass
        if rowcount is not None:
            try:
                span.set_attribute("db.rowcount", int(rowcount))
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            span.end()
        except Exception:
            pass


def current_trace_context_headers() -> dict:
    """把当前 W3C tracecontext 注出到 HTTP header dict（跨进程传递用，MR-2 用）。"""
    try:
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        headers = {}
        TraceContextTextMapPropagator().inject(headers)
        return headers
    except Exception:
        return {}


# ===== MR-2（2026-11-05）：任务生命周期 spans =====

def resume_trace_context(trace_context: str):
    """从任务行携带的 W3C traceparent 恢复上下文（worker/后续环节成为同一 trace 的子段）。

    trace_context 格式："traceparent|00-<traceid>-<spanid>-01"（inject 输出的 join）。
    无效/缺失/OTel 未启用 → 返回 None，调用方直接起新 span 或跳过。
    """
    try:
        if not otel_enabled() or not trace_context:
            return None
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        tp = trace_context.split("|", 1)[1] if "|" in trace_context else trace_context
        carrier = {"traceparent": tp}
        ctx = TraceContextTextMapPropagator().extract(carrier)
        # 校验恢复出有效 trace id（坏数据返回 INVALID）
        from opentelemetry.trace import INVALID_SPAN_CONTEXT, get_current_span
        span_ctx = get_current_span(ctx).get_span_context()
        if span_ctx.trace_id == 0:
            return None
        return ctx
    except Exception:
        return None


def start_task_span(name: str, attributes: dict, trace_context: str = ""):
    """任务生命周期 span 的统一入口。

    - trace_context 非空：resume 成同一 trace 的子 span（跨进程串联）
    - 为空：新 trace（enqueue 是根）
    返回 (span, ctx) 或 (None, None)（未启用/失败——调用方跳过，绝不阻塞业务）。
    用法:
        span, ctx = start_task_span("task.enqueue", {...})
        if span:
            try: ...业务...
            finally: span.end()
    """
    try:
        if not otel_enabled():
            return None, None
        init_otel("grape-task")  # 幂等：服务进程已被四入口初始化；独立调用（CLI/worker）自动补
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind, set_span_in_context
        ctx = None
        token = None
        if trace_context:
            ctx = resume_trace_context(trace_context)
        if ctx is not None:
            from opentelemetry import context as _otel_ctx
            token = _otel_ctx.attach(ctx)
        try:
            span = trace.get_tracer("grape.task").start_span(
                name, kind=SpanKind.INTERNAL, attributes={k: str(v)[:200] for k, v in attributes.items() if v is not None})
        finally:
            if token is not None:
                from opentelemetry import context as _otel_ctx
                _otel_ctx.detach(token)
        return span, ctx
    except Exception:
        return None, None


def snapshot_trace_context(span=None) -> str:
    """当前（或指定）span 的 W3C traceparent 序列化（存任务行 trace_context 列）。

    格式 "traceparent|<header 值>"（resume_trace_context 的逆操作）。
    无有效 span / 未启用 → ""。
    """
    try:
        if not otel_enabled():
            return ""
        from opentelemetry import trace
        sc = (span.get_span_context() if span is not None
              else trace.get_current_span().get_span_context())
        if not sc or not sc.is_valid:
            return ""
        return "traceparent|00-%s-%s-%s" % (
            format(sc.trace_id, "032x"),
            format(sc.span_id, "016x"),
            # 架构审查 P3：透传真实采样标志（00=未采样 / 01=已采样），
            # 硬编码 01 会在根未采样时让子段被 ParentBased 误导出
            format(sc.trace_flags, "02x"),
        )
    except Exception:
        return ""


def inject_trace_ids(record) -> None:
    """logging.LogRecord 注入 OTel trace_id/span_id（Step 3 日志关联）。

    由 config.py 的 JSON formatter 在 format 前调用；无有效 span 时回填业务
    trace_id（record.__dict__["business_trace_id"] 由调用方设置，如 task 级
    tr_<sha1(task_id)>），都没有则留空——老行为不变。
    """
    try:
        from opentelemetry import trace as _trace
        ctx = _trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            record.__dict__.setdefault("trace_id", format(ctx.trace_id, "032x"))
            record.__dict__.setdefault("span_id", format(ctx.span_id, "016x"))
            return
    except Exception:
        pass
    # 无 OTel span：回填业务 trace_id（若调用方给了）
    if "trace_id" not in record.__dict__:
        biz = record.__dict__.get("business_trace_id") or ""
        record.__dict__["trace_id"] = biz
    record.__dict__.setdefault("span_id", "")
