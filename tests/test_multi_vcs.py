"""Issue #49 多 VCS 扩展测试：事件归一化 + event_rule 平台维度 + routing。

测试 GitCode/GitHub/GitLab 三个 backend 的 webhook payload 归一化，
以及 event_rule.evaluate_event_action 的 vcs_source 维度规则匹配。
"""
import json
import pytest
from src.vcs.gitcode import GitCodeBackend
from src.vcs.github import GitHubBackend
from src.vcs.gitlab import GitLabBackend
from src.vcs.event_types import (
    ISSUE_OPENED, ISSUE_UPDATED, ISSUE_CLOSED, ISSUE_REOPENED,
    ISSUE_ASSIGNED, ISSUE_LABELED, ISSUE_COMMENT,
    PR_OPENED, PR_UPDATED, PR_CLOSED, PR_MERGED, PR_COMMENT,
    SKIP_EVENTS, SIDE_EFFECT_EVENTS, normalize_changes,
)


def _make_payload(**kw):
    """构造 GitCode webhook payload。"""
    return json.dumps(kw).encode("utf-8")


class TestGitCodeNormalize:
    """GitCode backend 事件归一化测试。"""

    def setup_method(self):
        self.backend = GitCodeBackend()

    def test_issue_open(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "open", "title": "Test Issue"},
            user_name="alice")
        evt = self.backend.normalize(raw, "Issue Hook")
        assert evt["type"] == ISSUE_OPENED
        assert evt["source"] == "gitcode"
        assert evt["repository"] == "owner/repo"
        assert evt["title"] == "Test Issue"
        assert evt["actor"] == "alice"

    def test_issue_close(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "close", "title": "Test"})
        evt = self.backend.normalize(raw, "Issue Hook")
        assert evt["type"] == ISSUE_CLOSED

    def test_issue_reopen(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "reopen", "title": "Test"})
        evt = self.backend.normalize(raw, "Issue Hook")
        assert evt["type"] == ISSUE_REOPENED

    def test_issue_assignee_change(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "update", "title": "Test"},
            changes={"assignees": [{"id": 1}]})
        evt = self.backend.normalize(raw, "Issue Hook")
        assert evt["type"] == ISSUE_ASSIGNED

    def test_issue_label_change(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "update", "title": "Test"},
            changes={"labels": [{"name": "bug"}]})
        evt = self.backend.normalize(raw, "Issue Hook")
        assert evt["type"] == ISSUE_LABELED

    def test_issue_content_update(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "update", "title": "Updated"},
            changes={"description": "new content"})
        evt = self.backend.normalize(raw, "Issue Hook")
        assert evt["type"] == ISSUE_UPDATED

    def test_note_issue_comment(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"noteable_type": "Issue", "note": "hello"},
            user={"username": "bob"})
        evt = self.backend.normalize(raw, "Note Hook")
        assert evt["type"] == ISSUE_COMMENT

    def test_note_pr_comment(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"noteable_type": "MergeRequest", "note": "review"})
        evt = self.backend.normalize(raw, "Note Hook")
        assert evt["type"] == PR_COMMENT

    def test_mr_merge(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "merge", "title": "PR #1"})
        evt = self.backend.normalize(raw, "Merge Request Hook")
        assert evt["type"] == PR_MERGED

    def test_mr_open(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "open", "title": "New PR"})
        evt = self.backend.normalize(raw, "Merge Request Hook")
        assert evt["type"] == PR_OPENED

    def test_mr_close(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "close", "title": "Closed PR"})
        evt = self.backend.normalize(raw, "Merge Request Hook")
        assert evt["type"] == PR_CLOSED

    def test_payload_has_raw_action_and_changes(self):
        raw = _make_payload(
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "update", "title": "Test"},
            changes={"assignees": []})
        evt = self.backend.normalize(raw, "Issue Hook")
        assert evt["payload"]["_raw_action"] == "update"
        assert "assignees" in evt["payload"]["_changes"]


class TestGitHubNormalize:
    """GitHub backend 事件归一化测试。"""

    def setup_method(self):
        self.backend = GitHubBackend()

    def test_issue_opened(self):
        raw = _make_payload(
            action="opened",
            repository={"full_name": "owner/repo"},
            issue={"title": "Bug", "number": 1},
            sender={"login": "alice"})
        evt = self.backend.normalize(raw, "issues")
        assert evt["type"] == ISSUE_OPENED
        assert evt["source"] == "github"

    def test_issue_closed(self):
        raw = _make_payload(
            action="closed",
            repository={"full_name": "owner/repo"},
            issue={"title": "Bug", "number": 1},
            sender={"login": "bob"})
        evt = self.backend.normalize(raw, "issues")
        assert evt["type"] == ISSUE_CLOSED

    def test_issue_reopened(self):
        raw = _make_payload(
            action="reopened",
            repository={"full_name": "owner/repo"},
            issue={"title": "Bug", "number": 1},
            sender={"login": "bob"})
        evt = self.backend.normalize(raw, "issues")
        assert evt["type"] == ISSUE_REOPENED

    def test_issue_assigned(self):
        raw = _make_payload(
            action="assigned",
            repository={"full_name": "owner/repo"},
            issue={"title": "Bug", "number": 1},
            sender={"login": "bob"})
        evt = self.backend.normalize(raw, "issues")
        assert evt["type"] == ISSUE_ASSIGNED

    def test_issue_edited(self):
        raw = _make_payload(
            action="edited",
            repository={"full_name": "owner/repo"},
            issue={"title": "Bug", "number": 1},
            changes={"body": {"from": "old"}},
            sender={"login": "bob"})
        evt = self.backend.normalize(raw, "issues")
        assert evt["type"] == ISSUE_UPDATED
        # body → description 统一映射
        assert "description" in evt["payload"]["_changes"]

    def test_pr_merged(self):
        raw = _make_payload(
            action="closed",
            repository={"full_name": "owner/repo"},
            pull_request={"title": "PR", "number": 1, "merged": True},
            sender={"login": "alice"})
        evt = self.backend.normalize(raw, "pull_request")
        assert evt["type"] == PR_MERGED

    def test_pr_closed_not_merged(self):
        raw = _make_payload(
            action="closed",
            repository={"full_name": "owner/repo"},
            pull_request={"title": "PR", "number": 1, "merged": False},
            sender={"login": "alice"})
        evt = self.backend.normalize(raw, "pull_request")
        assert evt["type"] == PR_CLOSED

    def test_pr_opened(self):
        raw = _make_payload(
            action="opened",
            repository={"full_name": "owner/repo"},
            pull_request={"title": "PR", "number": 1, "merged": False},
            sender={"login": "alice"})
        evt = self.backend.normalize(raw, "pull_request")
        assert evt["type"] == PR_OPENED

    def test_pr_synchronize(self):
        raw = _make_payload(
            action="synchronize",
            repository={"full_name": "owner/repo"},
            pull_request={"title": "PR", "number": 1, "merged": False},
            sender={"login": "alice"})
        evt = self.backend.normalize(raw, "pull_request")
        assert evt["type"] == PR_UPDATED


class TestGitLabNormalize:
    """GitLab backend 事件归一化测试。"""

    def setup_method(self):
        self.backend = GitLabBackend()

    def test_issue_open(self):
        raw = _make_payload(
            object_kind="issue",
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "open", "title": "Test"},
            user={"username": "alice"})
        evt = self.backend.normalize(raw, "Push Hook")  # header doesn't matter for GitLab
        assert evt["type"] == ISSUE_OPENED
        assert evt["source"] == "gitlab"

    def test_issue_close(self):
        raw = _make_payload(
            object_kind="issue",
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "close", "title": "Test"})
        evt = self.backend.normalize(raw, "Issue Hook")
        assert evt["type"] == ISSUE_CLOSED

    def test_issue_assignee_change(self):
        raw = _make_payload(
            object_kind="issue",
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "update", "title": "Test"},
            changes={"assignee_id": 123})
        evt = self.backend.normalize(raw, "Issue Hook")
        assert evt["type"] == ISSUE_ASSIGNED

    def test_mr_merge(self):
        raw = _make_payload(
            object_kind="merge_request",
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "merge", "title": "MR"})
        evt = self.backend.normalize(raw, "Merge Request Hook")
        assert evt["type"] == PR_MERGED

    def test_mr_close(self):
        raw = _make_payload(
            object_kind="merge_request",
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"action": "close", "title": "MR"})
        evt = self.backend.normalize(raw, "Merge Request Hook")
        assert evt["type"] == PR_CLOSED

    def test_note_comment(self):
        raw = _make_payload(
            object_kind="note",
            project={"path_with_namespace": "owner/repo"},
            object_attributes={"noteable_type": "Issue", "note": "hi"})
        evt = self.backend.normalize(raw, "Note Hook")
        assert evt["type"] == ISSUE_COMMENT


class TestEventTypes:
    """event_types.py 常量和工具函数测试。"""

    def test_skip_events_contains_closed_reopened_assigned(self):
        assert ISSUE_CLOSED in SKIP_EVENTS
        assert ISSUE_REOPENED in SKIP_EVENTS
        assert ISSUE_ASSIGNED in SKIP_EVENTS
        assert PR_CLOSED in SKIP_EVENTS

    def test_side_effect_contains_issue_closed(self):
        assert ISSUE_CLOSED in SIDE_EFFECT_EVENTS

    def test_normalize_changes_body_to_description(self):
        result = normalize_changes({"body": "new", "title": "updated"})
        assert "description" in result
        assert "title" in result

    def test_normalize_changes_assignee_to_assignees(self):
        result = normalize_changes({"assignee": {"id": 1}})
        assert "assignees" in result


class TestRouting:
    """routing.py 事件路由测试。"""

    def test_issue_opened_routes_to_vod(self):
        from src.api.routing import get_domain_agent
        assert get_domain_agent(ISSUE_OPENED) == "vod"

    def test_issue_closed_not_routable(self):
        from src.api.routing import get_domain_agent
        assert get_domain_agent(ISSUE_CLOSED) == ""

    def test_issue_reopened_not_routable(self):
        from src.api.routing import get_domain_agent
        assert get_domain_agent(ISSUE_REOPENED) == ""

    def test_issue_assigned_not_routable(self):
        from src.api.routing import get_domain_agent
        assert get_domain_agent(ISSUE_ASSIGNED) == ""

    def test_pr_closed_not_routable(self):
        from src.api.routing import get_domain_agent
        assert get_domain_agent(PR_CLOSED) == ""

    def test_issue_updated_routes_to_issue(self):
        from src.api.routing import get_domain_agent
        assert get_domain_agent(ISSUE_UPDATED) == "issue"

    def test_issue_comment_routes_to_issue(self):
        from src.api.routing import get_domain_agent
        assert get_domain_agent(ISSUE_COMMENT) == "issue"
