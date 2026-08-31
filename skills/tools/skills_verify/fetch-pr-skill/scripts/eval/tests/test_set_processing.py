#!/usr/bin/env python3
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_set_processing_sets_verify_status():
    import json

    d = tempfile.mkdtemp()
    ctx_dir = os.path.join(d, ".multica")
    os.makedirs(ctx_dir)
    with open(os.path.join(ctx_dir, "daemon_task_context.json"), "w") as f:
        json.dump({"issue_id": "issue-123"}, f)

    calls = []

    with mock.patch("os.getcwd", return_value=d):
        from eval.flow_status import get_multica_issue_id
        issue = get_multica_issue_id(d)
        assert issue == "issue-123"

    with mock.patch("eval.flow_status.update_verify_status", side_effect=lambda *a, **k: calls.append(("verify", a) or True)), \
         mock.patch("eval.flow_status.build_verify_url", return_value="https://url"):
        from eval.flow_status import update_verify_status, build_verify_url
        update_verify_status("issue-123", "processing", "https://url")

    statuses = [c[1][1] for c in calls]
    assert statuses == ["processing"]


def test_set_processing_no_issue_id():
    empty = tempfile.mkdtemp()
    calls = []

    with mock.patch("os.getcwd", return_value=empty), \
         mock.patch("eval.flow_status.update_verify_status", side_effect=lambda *a, **k: calls.append("verify") or True), \
         mock.patch("eval.flow_status.build_verify_url", return_value="https://url"):
        from eval.flow_status import get_multica_issue_id, update_verify_status, build_verify_url
        issue = get_multica_issue_id(os.getcwd())
        if issue:
            url = build_verify_url(issue, "")
            update_verify_status(issue, "processing", url)

    assert issue is None
    assert calls == [], calls
