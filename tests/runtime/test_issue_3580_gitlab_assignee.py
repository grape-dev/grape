"""Issue #3580 复审（对齐 PR #1854）：GitLab 按用户名指派。

GitLab 写接口只认数值 `assignee_ids`：基线把用户名字符串塞进 `assignee_id`
（上游 400/静默忽略），translate 层则直接 drop。本次解析序：
纯数字直接采用 → `GET /users?username=` 精确匹配取 id → 解析不到只告警不阻断。
其它平台（github/gitcode）保持原 `assignee` 字段语义，零回归。
"""
from __future__ import annotations

import pytest

from src.domains.runtime.vcs.api_client import VcsApiClient


@pytest.fixture()
def gitlab_client(monkeypatch):
    client = VcsApiClient("gitlab", "tok", "grp", "proj")
    calls: list = []

    def fake_request(method, path, data=None, form=False, timeout=30):
        calls.append((method, path, data))
        if str(path).startswith("/users"):
            return [{"id": 42, "username": "alice"}, {"id": 7, "username": "alice-2"}]
        return {"iid": 1}

    monkeypatch.setattr(client, "_request", fake_request)
    return client, calls


def _created_payload(calls):
    return [c for c in calls if c[0] == "POST"][-1][2]


def test_username_resolved_to_assignee_ids(gitlab_client):
    client, calls = gitlab_client
    client.create_issue("标题", "正文", assignee="alice")
    assert any(str(c[1]).startswith("/users?username=alice") for c in calls)
    assert _created_payload(calls)["assignee_ids"] == [42]


def test_numeric_assignee_is_passed_through(gitlab_client):
    client, calls = gitlab_client
    client.create_issue("标题", "正文", assignee="42")
    assert not any(str(c[1]).startswith("/users") for c in calls)
    assert _created_payload(calls)["assignee_ids"] == [42]


def test_unknown_assignee_is_dropped_not_sent_as_string(gitlab_client):
    client, calls = gitlab_client
    client.create_issue("标题", "正文", assignee="bob")
    payload = _created_payload(calls)
    assert "assignee_ids" not in payload
    assert "assignee_id" not in payload


def test_github_keeps_legacy_assignee_field(monkeypatch):
    client = VcsApiClient("github", "tok", "owner", "repo")
    calls: list = []

    def fake_request(method, path, data=None, form=False, timeout=30):
        calls.append((method, path, data))
        return {"number": 1}

    monkeypatch.setattr(client, "_request", fake_request)
    client.create_issue("t", "b", assignee="octocat")
    payload = _created_payload(calls)
    assert payload["assignee"] == "octocat"
    assert "assignee_ids" not in payload