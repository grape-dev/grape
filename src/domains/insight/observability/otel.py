"""可观察性 · OTel 基座（兼容入口）。

追踪基座已下沉到内核：``src/kernel/otel.py``（内核可用，import 期只依赖标准库）。
本模块保留为域内导入路径的薄转发，避免调用方在两种写法之间漂移：

    from src.domains.insight.observability.otel import instrument_fastapi

新代码请直接 ``from src.kernel.otel import ...``。
"""

from __future__ import annotations

from src.kernel.otel import (  # noqa: F401
    _RelayBackoffPolicy,
    _RelaySpanExporter,
    current_trace_context_headers,
    end_db_span,
    external_call_span,
    init_otel,
    instrument_fastapi,
    otel_enabled,
    resume_trace_context,
    snapshot_trace_context,
    start_db_span,
    start_external_span,
    relay_failure_kind,
    start_task_span,
)