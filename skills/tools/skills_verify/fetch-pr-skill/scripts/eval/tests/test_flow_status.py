#!/usr/bin/env python3
import os
import sys
import json
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.flow_status import (
    get_multica_issue_id,
    update_verify_status,
    build_verify_url,
)


def _make_workdir():
    d = tempfile.mkdtemp()
    ctx_dir = os.path.join(d, ".multica")
    os.makedirs(ctx_dir)
    with open(os.path.join(ctx_dir, "daemon_task_context.json"), "w") as f:
        json.dump({"managed_by": "multica-daemon-task", "issue_id": "abc-123"}, f)
    skill_dir = os.path.join(d, "my-skill")
    os.makedirs(skill_dir)
    return d, skill_dir


def test_get_multica_issue_id_from_parent_dir():
    _, skill_dir = _make_workdir()
    issue_id = get_multica_issue_id(skill_dir)
    assert issue_id == "abc-123"


def test_get_multica_issue_id_nested_deep():
    d, skill_dir = _make_workdir()
    deep = os.path.join(skill_dir, "sub", "deeper", "skill")
    os.makedirs(deep)
    assert get_multica_issue_id(deep) == "abc-123"


def test_get_multica_issue_id_returns_none_when_missing():
    tmp = tempfile.mkdtemp()
    assert get_multica_issue_id(tmp) is None


def test_get_multica_issue_id_from_cwd():
    d, _ = _make_workdir()
    with mock.patch("os.getcwd", return_value=d):
        issue_id = get_multica_issue_id("/nonexistent/path/skill")
    assert issue_id == "abc-123"


def test_get_multica_issue_id_falls_back_to_skill_path():
    d, skill_dir = _make_workdir()
    empty = tempfile.mkdtemp()
    with mock.patch("os.getcwd", return_value=empty):
        issue_id = get_multica_issue_id(skill_dir)
    assert issue_id == "abc-123"


def test_build_verify_url_always_returns_multica_url():
    assert build_verify_url("abc-123", "https://gitcode.com/o/r/pull/1") == \
        "https://multica.clouddeveloper.club/skills-prod/issues/abc-123"


def test_build_verify_url_without_pr_url():
    assert build_verify_url("abc-123") == \
        "https://multica.clouddeveloper.club/skills-prod/issues/abc-123"


def test_update_verify_status_calls_db():
    fake_conn = mock.MagicMock()
    fake_cursor = fake_conn.cursor.return_value.__enter__.return_value
    fake_cursor.rowcount = 1
    with mock.patch("eval.flow_status._get_connection", return_value=fake_conn):
        result = update_verify_status("abc-123", "done", "https://gitcode.com/o/r/pull/1")
    assert result is True
    sql = fake_cursor.execute.call_args[0][0]
    assert "node_verify_status" in sql
    assert "node_verify_time" in sql
    assert "multica_issue_id" in sql
    args = fake_cursor.execute.call_args[0][1]
    assert args[0] == "done"
    assert args[1] == "https://gitcode.com/o/r/pull/1"
    assert args[2] == "abc-123"


def test_update_verify_status_rejects_invalid_status():
    with mock.patch("eval.flow_status._get_connection") as fake:
        assert update_verify_status("abc-123", "pending") is False
        fake.assert_not_called()


def test_update_verify_status_returns_false_on_exception():
    with mock.patch("eval.flow_status._get_connection", side_effect=Exception("db down")):
        assert update_verify_status("abc-123", "done") is False
