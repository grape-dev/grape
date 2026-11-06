"""Log context fields for Grafana/Loki: repo name, issue id, event seq."""
from __future__ import annotations

import json
import logging

import pytest

from src.kernel.config import _JsonFormatter, _RequestIdFilter, log_context


def _format(message: str) -> dict:
    record = logging.LogRecord("devops-bot", logging.INFO, __file__, 1, message, None, None)
    _RequestIdFilter().filter(record)
    return json.loads(_JsonFormatter().format(record))


def test_log_context_binds_repo_issue_seq():
    with log_context(repo_key="gitcode:songspace/grape", issue_id="699", seq=1234):
        entry = _format("task step")

    assert entry["repo_key"] == "gitcode:songspace/grape"
    assert entry["issue_id"] == "699"
    assert str(entry["seq"]) == "1234"


def test_log_context_resets_after_block():
    with log_context(repo_key="gitcode:o/r", issue_id="#12", seq=7):
        pass

    entry = _format("after")
    assert entry["repo_key"] == ""
    assert entry["issue_id"] == ""
    assert str(entry["seq"]) == ""


def test_log_context_nested_restores_outer_values():
    with log_context(repo_key="outer", seq=1):
        with log_context(repo_key="inner", seq=2):
            assert _format("inner")["repo_key"] == "inner"
        entry = _format("outer")
    assert entry["repo_key"] == "outer"
    assert str(entry["seq"]) == "1"

def _fake_span(monkeypatch, *, valid: bool, trace_id: int = 0x0123456789ABCDEF0123456789ABCDEF,
               span_id: int = 0x0123456789ABCDEF):
    otel_trace = pytest.importorskip("opentelemetry.trace")

    ctx = type("_Ctx", (), {})()
    ctx.is_valid = valid
    ctx.trace_id = trace_id
    ctx.span_id = span_id

    class _Span:
        def get_span_context(self):
            return ctx

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: _Span())


def test_json_log_carries_trace_and_span_id(monkeypatch):
    _fake_span(monkeypatch, valid=True)
    data = _format("with trace")
    assert data["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert data["span_id"] == "0123456789abcdef"


def test_json_log_trace_id_empty_without_valid_span(monkeypatch):
    _fake_span(monkeypatch, valid=False)
    data = _format("no trace")
    assert data["trace_id"] == ""
    assert data["span_id"] == ""
