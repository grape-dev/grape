"""Grape 四进程 OpenTelemetry tracing 集成（可选启用）。

启用方式：设置环境变量 GRAPE_OTLP_ENDPOINT（如 http://127.0.0.1:4317）。
未设置时零开销（不导入 OTel、不注册 instrumentation）。

设计要点：
- 复用全局 TracerProvider；多进程（四进程分立 uvicorn）各自独立 Provider，
  service.name 用 GRAPE_TRACE_SERVICE（默认 grape-<role>）区分。
- FastAPI instrumentation 自动生成服务端 span（HTTP 请求），
  requests instrumentation 覆盖 worker/insight 对外的 LLM/SCM API 调用。
- 采样：默认 parentbased_traceidratio 0.2（20%），GRAPE_TRACE_RATIO 可调；
  排除健康探针与 /metrics 的噪声 span（excluded_urls）。
"""

from __future__ import annotations

import os
from typing import Optional


def otlp_enabled() -> bool:
    """GRAPE_OTLP_ENDPOINT 非空则启用 tracing。"""
    return bool(os.environ.get("GRAPE_OTLP_ENDPOINT", "").strip())


def setup_tracing(service_default: str) -> Optional[object]:
    """初始化 OTel tracing；返回 TracerProvider（未启用时返回 None）。

    幂等：重复调用（热重载）不会重复注册。
    """
    if not otlp_enabled():
        return None

    endpoint = os.environ["GRAPE_OTLP_ENDPOINT"].strip()
    service = os.environ.get("GRAPE_TRACE_SERVICE", "").strip() or service_default
    try:
        ratio = float(os.environ.get("GRAPE_TRACE_RATIO", "0.2"))
    except ValueError:
        ratio = 0.2

    # 幂等：已有全局 Provider（非 ProxyTracerProvider）则跳过
    from opentelemetry import trace
    existing = trace.get_tracer_provider()
    if type(existing).__name__ != "ProxyTracerProvider":
        return existing

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    resource = Resource.create({
        "service.name": service,
        "service.namespace": "grape",
        "deployment.environment": os.environ.get("GRAPE_TRACE_ENV", "dev"),
    })
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(ratio)),
    )
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # FastAPI instrumentation 需要 app 实例——先暂存 excluded_urls，
    # 由 instrument_fastapi_app(app) 在 app 构建完成后真正挂载。
    excluded = os.environ.get(
        "GRAPE_TRACE_EXCLUDE", "health,healthz,readyz,metrics,api/ping"
    ).split(",")
    globals()["_PENDING_FASTAPI_EXCLUDE"] = ",".join(p.strip() for p in excluded if p.strip())

    # requests 客户端 span（worker 调 LLM / insight 拉数据）
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        RequestsInstrumentor().instrument()
    except Exception:
        pass

    return provider


def instrument_fastapi_app(app) -> None:
    """在 app 构建完成后调用（setup_tracing 时 app 可能尚未建好）。"""
    pending = globals().get("_PENDING_FASTAPI_EXCLUDE")
    if not otlp_enabled() or pending is None:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app, excluded_urls=pending)
    globals()["_PENDING_FASTAPI_EXCLUDE"] = None
