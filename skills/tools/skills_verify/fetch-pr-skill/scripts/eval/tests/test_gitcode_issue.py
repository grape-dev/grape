#!/usr/bin/env python3
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.gitcode_issue import (
    get_pr_creator_login,
    create_report_issue,
    add_issue_label,
    close_issue,
    build_issue_body,
    REPORT_ISSUE_LABEL,
)


def test_get_pr_creator_login_success():
    with mock.patch("eval.gitcode_issue._http_json", return_value=[
        {"user": {"login": "alice", "name": "Alice"}}
    ]):
        r = get_pr_creator_login("o", "r", "7", "tok")
    assert r == "alice"


def test_get_pr_creator_login_empty_returns_none():
    with mock.patch("eval.gitcode_issue._http_json", return_value=[]):
        assert get_pr_creator_login("o", "r", "7", "tok") is None


def test_get_pr_creator_login_request_shape():
    with mock.patch("eval.gitcode_issue._http_json", return_value=[{"user": {"login": "alice"}}]) as fake:
        get_pr_creator_login("o", "r", "7", "tok")
        method, url, headers = fake.call_args[0][:3]
        assert method == "GET"
        assert url == "https://api.gitcode.com/api/v5/repos/o/r/pulls/7/issues"
        assert headers["PRIVATE-TOKEN"] == "tok"


def test_create_report_issue_returns_number():
    with mock.patch("eval.gitcode_issue._http_json", return_value={"number": 42}):
        assert create_report_issue("o", "r", "t", "b", "tok") == 42


def test_create_report_issue_request_shape():
    with mock.patch("eval.gitcode_issue._http_json", return_value={"number": 42}) as fake:
        create_report_issue("o", "r", "t", "b", "tok")
        method, url, headers, body = fake.call_args[0]
        assert method == "POST"
        assert url == "https://api.gitcode.com/api/v5/repos/o/r/issues"
        assert headers["PRIVATE-TOKEN"] == "tok"
        assert body == {"title": "t", "body": "b"}


def test_create_report_issue_no_number_returns_none():
    with mock.patch("eval.gitcode_issue._http_json", return_value={}):
        assert create_report_issue("o", "r", "t", "b", "tok") is None


def test_add_issue_label_request_shape():
    with mock.patch("eval.gitcode_issue._http_json") as fake:
        add_issue_label("o", "r", 42, "评测参考", "tok")
        method, url, headers, body = fake.call_args[0]
        assert method == "PATCH"
        assert url == "https://api.gitcode.com/api/v5/repos/o/r/issues/42"
        assert headers["PRIVATE-TOKEN"] == "tok"
        assert body == {"labels": "评测参考"}


def test_close_issue_request_shape():
    with mock.patch("eval.gitcode_issue._http_json") as fake:
        close_issue("o", "r", 42, "tok")
        method, url, headers, body = fake.call_args[0]
        assert method == "PATCH"
        assert url == "https://api.gitcode.com/api/v5/repos/o/r/issues/42"
        assert body == {"state": "close"}


def test_build_issue_body_includes_creator_report_url_and_scores():
    with mock.patch("eval.gitcode_issue.build_report_content", return_value="# 报告内容"):
        body = build_issue_body(
            "skill-a", "https://gitcode.com/o/r/merge_requests/1",
            None, None, "high", "r1", "http://x/report.md", "alice",
        )
    assert "@alice 评测完成" in body
    assert "# 报告内容" in body
    assert "http://x/report.md" in body


def test_build_issue_body_without_creator():
    with mock.patch("eval.gitcode_issue.build_report_content", return_value="# 报告内容"):
        body = build_issue_body(
            "skill-a", "https://gitcode.com/o/r/merge_requests/1",
            None, None, "high", "r1", "http://x/report.md", None,
        )
    assert "@" not in body


def test_report_issue_label_constant():
    assert REPORT_ISSUE_LABEL == "评测参考"


def test_build_issue_body_without_attachment_url_param():
    with mock.patch("eval.gitcode_issue.build_report_content", return_value="# report"):
        body = build_issue_body(
            "skill-a", "https://gitcode.com/o/r/merge_requests/1",
            None, None, "high", "r1", "http://x/report.md", "alice",
        )
    assert "评测报告附件" not in body
    assert "下载完整报告" not in body
    assert "http://x/report.md" in body
