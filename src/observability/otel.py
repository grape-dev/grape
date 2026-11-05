"""可观察性 · OTel 基座（LGTM Traces，2026-11-05 MR-1 Step 1）。

单一初始化源：A/B/C/webhook 四入口 import 后各自获得独立 service.name 的
TracerProvider + OTLP gRPC exporter + FastAPI 自动 HTTP span。

设计要点（docs/weekly-10/full-observability-tracing-plan.md）：
- OTEL_EXPORTER_OTLP_ENDPOINT（grape.env）非空才启用；缺省禁用零开销
- 尾部采样：正常流量按 OTEL_SAMPLER_RATIO（默认 0.1）抽；错误/慢请求全采
  ——先保护 Tempo 14 天盘，后续按量调参
- 失败不致命：全链 try/except，Tempo 断网/超时对业务零影响（exporter 异步）
- service.name 由调用方传入（grape-server-a/b/c、grape-webhook、grape-worker），
  resource 统一补 host/env/version

与平台既有确定性 trace id 体系（src/observability/trace.py，tr_<sha1(task_id)>）
的关系：MR-1 仅 HTTP span 与日志注入（OTel 自动生成 trace id）；MR-2 把任务
生命周期接入，task_id/pipeline_trace_id 作为 span 属性与业务 id 同源对齐。
"""
import os
from contextlib import contextmanager

_inited_service = None


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
    """初始化本进程的 OTel。幂等（同 service 重复调用直接返回已初始化 Provider）。

    返回 TracerProvider（成功）/ None（未启用或初始化失败——调用方跳过埋点）。
    """
    global _inited_service
    try:
        if not otel_enabled():
            return None
        if _inited_service == service_name:
            from opentelemetry.trace import get_tracer_provider
            return get_tracer_provider()

        from opentelemetry import trace, metrics
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased, ParentBased

        resource = Resource.create({
            "service.name": service_name,
            "service.version": os.environ.get("GIT_COMMIT", "dev"),
            "host": os.environ.get("HOSTNAME", os.uname().nodename),
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
        return provider
    except Exception as e:
        try:
            from src.config import log
            log.warning(f"OTel init failed (service={service_name}, tracing disabled): {e}")
        except Exception:
            pass
        return None


def instrument_fastapi(app, service_name: str):
    """FastAPI 自动 HTTP span（A/B/C/webhook 四入口通用）。幂等、失败不致命。"""
    try:
        if not otel_enabled():
            return False
        init_otel(service_name)
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        # 剔除探针/指标路径，避免无意义 span 噪声（与 metrics 中间件同款口径）
        # 噪声端点剔除（2026-11-05）：worker 每 15s 的 claim/heartbeat/tags 刷屏 Tempo
        # 列表，把真正要看的任务 trace 挤出搜索窗口。任务生命周期另有专门 span
        # （task.*），协议面已在 metrics（/metrics）观测，trace 层保留完整请求以外的价值。
        FastAPIInstrumentor.instrument_app(app, excluded_urls=(
            "health,healthz,readyz,metrics,"
            "runtimes/[^/]+/tasks/claim,runtimes/[^/]+/tags,"
            "/runtime/heartbeat,/runtime/register,"
            "grape-worker[.]tar[.]gz,/runtime/install"
        ))
        # DB span（2026-11-05）：pymysql 自动插桩——每条 SQL 一个 span（statement/耗时），
        # 让调用链瀑布能看到"请求 → DB 查询"最深一层。PyMySQL 官方插桩按 connection patch，
        # 对本项目 mysql_compat.py 的直连/池化均生效。失败不致命。
        try:
            from opentelemetry.instrumentation.pymysql import PyMySQLInstrumentor
            PyMySQLInstrumentor().instrument()
        except Exception as e:
            try:
                from src.config import log
                log.warning(f"PyMySQL OTel instrument failed (DB spans disabled): {e}")
            except Exception:
                pass
        return True
    except Exception as e:
        try:
            from src.config import log
            log.warning(f"FastAPI OTel instrument failed ({service_name}): {e}")
        except Exception:
            pass
        return False


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
        if ctx.get("current-span", None) is None:
            pass
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
        return "traceparent|00-%s-%s-01" % (format(sc.trace_id, "032x"), format(sc.span_id, "016x"))
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
