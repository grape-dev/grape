"""请求日志 trace_id 兜底（grape-mgmt 日志 trace_id 全空的回归）。

现象：mgmt 的请求汇总日志（GET /api/... status=200 duration=...）trace_id 恒为空，
而 Tempo 里同一请求确实有 grape-mgmt span。

根因：日志在 Starlette BaseHTTPMiddleware 的 `await call_next()` 之后发出；下游 app 在
独立 task 里激活 span，回到中间件时 already 无当前 span → _current_span_ids() 返回空。

修复：中间件在入口解析 W3C traceparent 写入 contextvars，日志过滤器优先取当前 span，
缺失时回退到这两个变量。
"""
import logging
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.api.server_common import _trace_ids_from_traceparent  # noqa: E402
from src.kernel.config import (  # noqa: E402
    _RequestIdFilter,
    span_id_var,
    trace_id_var,
)

TRACE = "0123456789abcdef0123456789abcdef"
SPAN = "fedcba9876543210"


def test_parse_valid_traceparent():
    assert _trace_ids_from_traceparent(f"00-{TRACE}-{SPAN}-01") == (TRACE, SPAN)


def test_parse_uppercase_normalized():
    assert _trace_ids_from_traceparent(f"00-{TRACE.upper()}-{SPAN.upper()}-01") == (TRACE, SPAN)


def test_parse_invalid_values():
    for bad in ("", "00", "00-short-span-01", f"00-{TRACE}-short-01",
                f"00-{TRACE}-{SPAN}", "00-" + "z" * 32 + f"-{SPAN}-01"):
        assert _trace_ids_from_traceparent(bad) == ("", ""), bad


def test_filter_falls_back_to_context_vars():
    """无当前 span 时，过滤器用中间件写入的 ContextVar。"""
    tok_t = trace_id_var.set(TRACE)
    tok_s = span_id_var.set(SPAN)
    try:
        record = logging.LogRecord("devops-bot", logging.INFO, __file__, 1, "hello", None, None)
        assert _RequestIdFilter().filter(record) is True
        assert getattr(record, "trace_id") == TRACE
        assert getattr(record, "span_id") == SPAN
    finally:
        trace_id_var.reset(tok_t)
        span_id_var.reset(tok_s)


def test_filter_empty_without_span_or_vars():
    record = logging.LogRecord("devops-bot", logging.INFO, __file__, 1, "hello", None, None)
    _RequestIdFilter().filter(record)
    assert getattr(record, "trace_id") == ""
    assert getattr(record, "span_id") == ""