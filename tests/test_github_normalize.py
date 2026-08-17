"""GitHub Webhook 事件归一化单元测试"""
import json

import pytest

from src.vcs.github import GitHubBackend


def _make_payload(event_type: str, overrides: dict = None) -> tuple[bytes, dict]:
    """构造模拟 GitHub Webhook 请求"""
    base = {
        "push": {
            "ref": "refs/heads/main",
            "repository": {"full_name": "org/repo", "name": "repo"},
            "sender": {"login": "alice"},
            "commits": [{"message": "fix bug"}],
        },
        "issues": {
            "action": "opened",
            "issue": {"number": 42, "title": "Bug", "user": {"login": "bob"}},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "bob"},
        },
        "pull_request": {
            "action": "opened",
            "pull_request": {"number": 128, "title": "Fix", "base": {"ref": "main"}},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice"},
        },
        "release": {
            "action": "published",
            "release": {"tag_name": "v1.0", "name": "v1.0"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice"},
        },
        "issue_comment": {
            "action": "created",
            "issue": {"number": 42, "user": {"login": "bob"}},
            "comment": {"body": "LGTM"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "bob"},
        },
    }

    body = base.get(event_type, {"repository": {"full_name": "org/repo"}, "sender": {"login": "test"}})
    if overrides:
        body.update(overrides)

    return json.dumps(body).encode(), body


@pytest.fixture(scope="module")
def backend():
    return GitHubBackend()


@pytest.mark.parametrize("header,expected_type,expected_branch,expected_actor", [
    ("push", "push", "main", "alice"),
    ("issues", "issue_opened", "", "bob"),
    ("pull_request", "pull_request_opened", "main", "alice"),
    ("release", "release_created", "", "alice"),
    ("issue_comment", "issue_comment", "", "bob"),
])
def test_normalize_fields(backend, header, expected_type, expected_branch, expected_actor):
    raw, _ = _make_payload(header)
    result = backend.normalize(raw, header)
    assert result["type"] == expected_type
    assert result["repository"] == "org/repo"
    assert result["branch"] == expected_branch
    assert result["actor"] == expected_actor
    assert result["source"] == "github"


def test_unknown_event_type_falls_back(backend):
    raw, _ = _make_payload("some_future_event")
    result = backend.normalize(raw, "some_future_event")
    assert result["repository"] == "org/repo"
    assert result["source"] == "github"
