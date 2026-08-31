#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.gitcode_merger import parse_pr_url


def test_parse_pr_url_pull_format():
    r = parse_pr_url("https://gitcode.com/developer-skill/huaweicloud-skills-dev/pull/101")
    assert r == {"owner": "developer-skill", "repo": "huaweicloud-skills-dev", "pr_number": "101"}


def test_parse_pr_url_merge_requests_format():
    r = parse_pr_url("https://gitcode.com/developer-skill/huaweicloud-skills-dev/merge_requests/101")
    assert r == {"owner": "developer-skill", "repo": "huaweicloud-skills-dev", "pr_number": "101"}


def test_parse_pr_url_invalid_returns_none():
    assert parse_pr_url("https://example.com/foo") is None
    assert parse_pr_url("") is None
