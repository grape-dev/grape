"""Log context fields for Grafana/Loki: repo name, issue id, event seq."""
from __future__ import annotations

import json
import logging

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