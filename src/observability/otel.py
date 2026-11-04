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

_inited_service = None


def otel_enabled() -> bool:
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


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
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

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
        exporter = OTLPSpanExporter(
            endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"].strip(),
            timeout=3,
        )
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
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,healthz,readyz,metrics")
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
