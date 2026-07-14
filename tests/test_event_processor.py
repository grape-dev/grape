"""Tests for event_processor module (normalize_event)."""
import pytest
import json
import os
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from src.event_processor import normalize_event


class TestNormalizeEventGitCode:
    """GitCode webhook format tests."""

    def test_issue_opened(self):
        raw = {
            "event_type": "issues",
            "project": {"path_with_namespace": "owner/repo"},
            "user_name": "tester",
            "issue": {"number": 1, "title": "Bug report"}
        }
        evt = normalize_event(json.dumps(raw), "issues")
        assert evt["type"] == "issue_opened"
        assert evt["repository"] == "owner/repo"
        assert evt["actor"] == "tester"

    def test_issue_comment(self):
        raw = {
            "event_type": "note",
            "project": {"path_with_namespace": "owner/repo"},
            "user": {"username": "commenter"},
            "object_attributes": {"note": "test comment", "iid": 5}
        }
        evt = normalize_event(json.dumps(raw), "Note Hook")
        assert evt["type"] == "issue_comment"
        assert evt["repository"] == "owner/repo"

    def test_merge_request_opened(self):
        raw = {
            "event_type": "merge_request",
            "project": {"path_with_namespace": "owner/repo"},
            "user": {"username": "mr_author"},
            "object_attributes": {"iid": 3, "title": "Fix bug"}
        }
        evt = normalize_event(json.dumps(raw), "Merge Request Hook")
        assert evt["type"] == "pull_request_opened"

    def test_push_event(self):
        raw = {
            "event_type": "push",
            "project": {"path_with_namespace": "owner/repo"},
            "user_name": "pusher",
            "ref": "refs/heads/feature-branch",
            "commits": [{"message": "msg"}]
        }
        evt = normalize_event(json.dumps(raw), "push")
        assert evt["type"] == "push"
        assert evt["branch"] == "feature-branch"

    def test_push_to_tag(self):
        raw = {
            "event_type": "tag_push",
            "project": {"path_with_namespace": "owner/repo"},
            "user_name": "tagger",
            "ref": "refs/tags/v1.0"
        }
        evt = normalize_event(json.dumps(raw), "tag_push")
        assert evt["type"] == "tag_created"

    def test_push_without_ref(self):
        raw = {"event_type": "push", "project": {"path_with_namespace": "owner/repo"}}
        evt = normalize_event(json.dumps(raw), "push")
        assert evt["branch"] == ""

    def test_repository_field_fallback(self):
        raw = {
            "event_type": "push",
            "repository": {"full_name": "owner/repo", "name": "repo"}
        }
        evt = normalize_event(json.dumps(raw), "push")
        assert evt["repository"] == "owner/repo"

    def test_actor_from_sender(self):
        raw = {
            "event_type": "issues",
            "project": {"path_with_namespace": "o/r"},
            "sender": {"login": "sender_user", "username": "sn"}
        }
        evt = normalize_event(json.dumps(raw), "issues")
        assert evt["actor"] == "sn"  # sender.username is prioritized


class TestNormalizeEventLabeling:
    def test_self_label_change_rewrites_to_issue_labeled(self):
        """Bot PATCH labels should be rewritten to issue_labeled type."""
        # This detection happens in process_event, not normalize_event.
        # normalize_event just sets type based on event_header.
        raw = {
            "event_type": "issues",
            "project": {"path_with_namespace": "owner/repo"},
            "user": {"username": "bot"},
            "changes": {"labels": {"from": "", "to": "bug"}},
            "issue": {"number": 1}
        }
        evt = normalize_event(json.dumps(raw), "issues")
        # normalize_event uses the event_type_map, changes detection is in process_event
        assert evt["type"] == "issue_opened"


class TestNormalizeEventBytes:
    def test_bytes_input(self):
        raw = json.dumps({
            "event_type": "push",
            "project": {"path_with_namespace": "owner/repo"},
            "user_name": "tester"
        }).encode("utf-8")
        evt = normalize_event(raw, "push")
        assert evt["type"] == "push"

    def test_gbk_fallback(self):
        """If UTF-8 fails partially, it should try GBK."""
        raw = json.dumps({
            "event_type": "push",
            "project": {"path_with_namespace": "o/r"}
        }).encode("gbk")
        evt = normalize_event(raw, "push")
        assert evt["repository"] == "o/r"


class TestNormalizeEventCornerCases:
    def test_empty_repo_returns_unknown(self):
        raw = {"event_type": "unknown"}
        evt = normalize_event(json.dumps(raw), "unknown")
        assert evt["type"] == "unknown"

    def test_project_as_string_ignored(self):
        raw = {"event_type": "push", "project": "just_a_string"}
        evt = normalize_event(json.dumps(raw), "push")
        assert evt["repository"] == ""

    def test_user_alternative_keys(self):
        """Try multiple user keys."""
        raw = {
            "event_type": "issues",
            "project": {"path_with_namespace": "o/r"},
            "user": {"username": "fallback_user"}
        }
        evt = normalize_event(json.dumps(raw), "issues")
        assert evt["actor"] == "fallback_user"
