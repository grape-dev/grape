"""Request-scoped log fields: issue id / event seq extracted from the URL."""
from __future__ import annotations

from starlette.datastructures import QueryParams

from src.api.server_common import _issue_seq_from_request, _normalize_issue


def test_issue_id_from_issue_path():
    assert _issue_seq_from_request("/api/issues/25/status", QueryParams("")) == ("#25", "")


def test_issue_id_from_query_params():
    assert _issue_seq_from_request("/api/issues", QueryParams("issue=25")) == ("#25", "")
    assert _issue_seq_from_request("/api/anything", QueryParams("issue_number=%2325")) == ("#25", "")


def test_seq_from_event_path_and_query():
    assert _issue_seq_from_request("/api/events/9474", QueryParams("")) == ("", "9474")
    assert _issue_seq_from_request("/api/events", QueryParams("seq=9474")) == ("", "9474")


def test_issue_path_and_seq_path_are_independent():
    assert _issue_seq_from_request("/api/issues/25/status", QueryParams("seq=12")) == ("#25", "12")


def test_normalize_issue_keeps_non_numeric_ids():
    assert _normalize_issue("#25") == "#25"
    assert _normalize_issue("25") == "#25"
    assert _normalize_issue("issue-abc") == "issue-abc"
    assert _normalize_issue("") == ""