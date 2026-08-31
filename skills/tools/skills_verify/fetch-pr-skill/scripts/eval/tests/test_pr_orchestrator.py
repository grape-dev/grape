#!/usr/bin/env python3
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.pr_orchestrator import run_pr_orchestration


def _base_kwargs():
    return {
        "issue_id": "issue-1",
        "pr_url": "https://gitcode.com/o/r/merge_requests/7",
        "skill_name": "test-skill",
        "eval_result": None,
        "metrics": None,
        "security_risk": "low",
        "report_id": "report_001",
        "report_url": "http://skills.topxtopx.com/report/test.md",
    }


def test_orchestration_creates_issue_and_label_close():
    calls = []
    parsed = {"owner": "o", "repo": "r", "pr_number": "7"}
    with mock.patch.dict(os.environ, {"GITCODE_TOKEN": "tok"}, clear=False), \
         mock.patch("eval.pr_orchestrator.parse_pr_url", return_value=parsed), \
         mock.patch("eval.pr_orchestrator.get_pr_creator_login", return_value="alice"), \
         mock.patch("eval.pr_orchestrator.build_issue_body", return_value="body"), \
         mock.patch("eval.pr_orchestrator.create_report_issue", side_effect=lambda *a, **k: calls.append("create") or 42), \
         mock.patch("eval.pr_orchestrator.add_issue_label", side_effect=lambda *a, **k: calls.append("label") or True), \
         mock.patch("eval.pr_orchestrator.close_issue", side_effect=lambda *a, **k: calls.append("close") or True):
        run_pr_orchestration(**{**_base_kwargs(), "eval_ok": True})
    assert calls == ["create", "label", "close"], calls


def test_orchestration_skips_issue_when_eval_fails():
    issue_called = []
    parsed = {"owner": "o", "repo": "r", "pr_number": "7"}
    with mock.patch.dict(os.environ, {"GITCODE_TOKEN": "tok"}, clear=False), \
         mock.patch("eval.pr_orchestrator.parse_pr_url", return_value=parsed), \
         mock.patch("eval.pr_orchestrator.create_report_issue", side_effect=lambda *a, **k: issue_called.append(1) or 42):
        run_pr_orchestration(**{**_base_kwargs(), "eval_ok": False})
    assert issue_called == [], "eval_ok=False must skip issue creation"


def test_orchestration_skips_all_without_token():
    parsed = {"owner": "o", "repo": "r", "pr_number": "7"}
    with mock.patch.dict(os.environ, {}, clear=True), \
         mock.patch("eval.pr_orchestrator.parse_pr_url", return_value=parsed), \
         mock.patch("eval.pr_orchestrator.create_report_issue") as fake_issue:
        run_pr_orchestration(**{**_base_kwargs(), "eval_ok": True})
    fake_issue.assert_not_called()


def test_orchestration_skips_all_without_pr_url():
    with mock.patch.dict(os.environ, {"GITCODE_TOKEN": "tok"}, clear=False), \
         mock.patch("eval.pr_orchestrator.create_report_issue") as fake_issue:
        run_pr_orchestration(**{**_base_kwargs(), "pr_url": "", "eval_ok": True})
    fake_issue.assert_not_called()


def test_orchestration_skips_all_on_dry_run():
    parsed = {"owner": "o", "repo": "r", "pr_number": "7"}
    with mock.patch.dict(os.environ, {"GITCODE_TOKEN": "tok"}, clear=False), \
         mock.patch("eval.pr_orchestrator.parse_pr_url", return_value=parsed), \
         mock.patch("eval.pr_orchestrator.create_report_issue") as fake_issue:
        run_pr_orchestration(**{**_base_kwargs(), "eval_ok": True, "dry_run": True})
    fake_issue.assert_not_called()


def test_orchestration_no_pr_url_parse_skips():
    with mock.patch.dict(os.environ, {"GITCODE_TOKEN": "tok"}, clear=False), \
         mock.patch("eval.pr_orchestrator.parse_pr_url", return_value=None), \
         mock.patch("eval.pr_orchestrator.create_report_issue") as fake_issue:
        run_pr_orchestration(**{**_base_kwargs(), "eval_ok": True})
    fake_issue.assert_not_called()


def test_orchestration_creator_lookup_raise_falls_back_to_none():
    issue_called = []
    body_built = []
    parsed = {"owner": "o", "repo": "r", "pr_number": "7"}
    with mock.patch.dict(os.environ, {"GITCODE_TOKEN": "tok"}, clear=False), \
         mock.patch("eval.pr_orchestrator.parse_pr_url", return_value=parsed), \
         mock.patch("eval.pr_orchestrator.get_pr_creator_login", side_effect=Exception("network down")), \
         mock.patch("eval.pr_orchestrator.build_issue_body", side_effect=lambda *a, **k: body_built.append(a[7]) or "body"), \
         mock.patch("eval.pr_orchestrator.create_report_issue", side_effect=lambda *a, **k: issue_called.append(1) or 42), \
         mock.patch("eval.pr_orchestrator.add_issue_label", return_value=True), \
         mock.patch("eval.pr_orchestrator.close_issue", return_value=True):
        run_pr_orchestration(**{**_base_kwargs(), "eval_ok": True})
    assert issue_called == [1], "creator lookup failure must not abort ISSUE phase"
    assert body_built == [None], "creator must fall back to None on lookup failure"
