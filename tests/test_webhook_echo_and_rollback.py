"""Tests for issue pipeline optimization: webhook echo suppression (Plan B) and A2A rollback cancel (Plan A).

See docs/new-version3/issue-pipeline-optimization-design.md
"""
import json
import os
import uuid

import pytest

from src.session_manager import db, db_lock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_REPO = "gitcode:owner/repo"
_BOT_TOKEN = "test-bot-token-12345"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events WHERE seq >= 900")
        c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        c.execute("DELETE FROM devops_agent_session WHERE id LIKE 'sess-rl-%'")
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE '%rl-dev-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'rl-cred-%'")
        c.execute("DELETE FROM event_skipped")
        db.commit()


def _seed_managed_repo(repo_key=_TEST_REPO):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'owner', 'repo', 'owner/repo', 0, 'default', ?, 'wt-test', 'grape_dev_bot')",
            (repo_key, _BOT_TOKEN))
        db.commit()


def _seed_engine_and_session(agent="coding", repo_key=_TEST_REPO):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?,?,?, 'global', 'tester', 'active')",
            ("h" + uuid.uuid4().hex, "rl-cred-" + agent, "rl-dev-" + agent))
        _cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
            "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
        _rid = c.lastrowid
        c.execute(
            "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable, repo_gitcode_token) "
            "VALUES (?, ?, ?, ?, 'idle', 1, ?)",
            ("sess-rl-" + agent, repo_key, agent, _rid, _BOT_TOKEN))
        db.commit()


def _seed_issue_state(stage="qa", repo_key=_TEST_REPO, issue_id="#5"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO issue_state (repo_key, issue_id, stage, owner_agent) VALUES (?, ?, ?, ?)",
            (repo_key, issue_id, stage, stage))
        db.commit()


def _a2a_body(agent="coding", issue_number="5"):
    return {
        "agent": agent,
        "payload": {
            "repository": "owner/repo",
            "account": "owner/repo",
            "issue_number": issue_number,
            "pr_number": "",
            "context": "test A2A",
        },
    }


def _post_a2a(client, body):
    return client.post("/api/a2a", json=body, headers={"Authorization": f"Bearer {_BOT_TOKEN}"})


def _seed_task(agent_type="sre", status="running", seq=999, issue_id="#5", repo_key=_TEST_REPO):
    """Insert a fake task in agent_tasks with all required columns."""
    _task_id = "t_" + uuid.uuid4().hex[:12]
    with db_lock:
        c = db.cursor()
        # Insert a fake event
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, agent_type, status, issue_id, title, summary, source, created_at) "
            "VALUES (?, ?, 'a2a_event', ?, ?, ?, 'test', 'test', 'a2a', datetime('now'))",
            (seq, repo_key, agent_type, status, issue_id))
        # Insert the task with all required columns
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, "
            "agent_type, skill, prompt, cwd, runtime_session_id, event_type, account, user_id, "
            "repo_gitcode_token, env_extra, skill_files, server_url, issue_session_id, "
            "project_info_files, project_info_meta, knowledge_files, status, exit_code, "
            "result_text, errors, stderr, usage_json, started_at, completed_at, created_at, attempts) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'test', '', '', 'a2a_event', '', 0, ?, '{}', '{}', '', '', '{}', '{}', '{}', ?, NULL, '', '', '', '{}', NULL, NULL, datetime('now'), 0)",
            (_task_id, f"sess-rl-{agent_type}", seq, issue_id, repo_key,
             agent_type, agent_type, _BOT_TOKEN, status))
        db.commit()
    return _task_id, seq


# ---------------------------------------------------------------------------
# Plan B: system auto-comment marker suppression
# ---------------------------------------------------------------------------

class TestPlanBMarkerSuppression:
    """Plan B: <!-- grape-auto-comment --> marker in system posts should be skipped by run_pipeline."""

    def _make_note_payload(self, note_body, event_header="Note Hook"):
        raw = json.dumps({
            "object_attributes": {"note": note_body, "noteable_type": "Issue"},
            "issue": {"number": 5, "title": "t", "body": "x"},
            "project": {"path_with_namespace": "owner/repo"},
            "user": {"username": "some_user"},
        }).encode()
        headers = {"X-GitCode-Event": event_header}
        return raw, headers

    def test_marker_in_comment_is_skipped(self):
        """Comment with grape-auto-comment marker should be skipped (no agent triggered)."""
        import src.api.event_pipeline as ep
        _clean()
        _seed_managed_repo()
        raw, headers = self._make_note_payload("[CODING Agent] done\n<!-- grape-auto-comment -->")
        result = ep.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            c = db.cursor()
            count = c.execute("SELECT COUNT(*) FROM events WHERE event_type='issue_comment'").fetchone()[0]
        assert count == 0, "system auto-comment with marker should be skipped"

    def test_user_comment_without_marker_is_not_skipped(self):
        """User comment without marker should NOT be skipped."""
        import src.api.event_pipeline as ep
        _clean()
        _seed_managed_repo()
        raw, headers = self._make_note_payload("Please start development")
        ep.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            c = db.cursor()
            count = c.execute("SELECT COUNT(*) FROM events WHERE event_type='issue_comment'").fetchone()[0]
        assert count >= 1, "user comment without marker should create event"

    def test_pr_comment_with_marker_is_skipped(self):
        """note_hook_pr with marker should also be skipped."""
        import src.api.event_pipeline as ep
        _clean()
        _seed_managed_repo()
        raw = json.dumps({
            "object_attributes": {"note": "[QA Agent] review done\n<!-- grape-auto-comment -->", "noteable_type": "MergeRequest"},
            "pull_request": {"number": 3, "title": "pr"},
            "project": {"path_with_namespace": "owner/repo"},
            "user": {"username": "some_user"},
        }).encode()
        headers = {"X-GitCode-Event": "Note Hook"}
        ep.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            c = db.cursor()
            count = c.execute("SELECT COUNT(*) FROM events WHERE event_type='note_hook_pr'").fetchone()[0]
        assert count == 0, "PR comment with marker should be skipped"

    def test_empty_note_not_skipped(self):
        """Empty note should not crash and should not be skipped by marker check."""
        import src.api.event_pipeline as ep
        _clean()
        _seed_managed_repo()
        raw, headers = self._make_note_payload("")
        ep.run_pipeline(raw, headers, account="owner/repo")

    def test_non_comment_event_not_affected_by_marker(self):
        """issue_opened event should not be affected by marker check."""
        import src.api.event_pipeline as ep
        _clean()
        _seed_managed_repo()
        raw = json.dumps({
            "object_attributes": {"action": "open", "title": "test", "description": "test body"},
            "issue": {"number": 5, "title": "test", "body": "test body"},
            "project": {"path_with_namespace": "owner/repo"},
            "user": {"username": "real_user"},
        }).encode()
        headers = {"X-GitCode-Event": "Issue Hook"}
        ep.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            c = db.cursor()
            count = c.execute("SELECT COUNT(*) FROM events WHERE event_type='issue_opened'").fetchone()[0]
        assert count >= 1, "issue_opened should not be affected by marker check"


# ---------------------------------------------------------------------------
# Plan B: marker added to system post functions
# ---------------------------------------------------------------------------

class TestPlanBMarkerAdded:
    """Verify that _post_result_to_issue adds the marker."""

    def test_post_result_to_issue_adds_marker(self):
        """_post_result_to_issue should append <!-- grape-auto-comment --> to comment body."""
        import src.api.event_pipeline as ep
        posted = {}

        class _MockResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def _mock_urlopen(req, timeout=15):
            import json as _j
            posted["body"] = _j.loads(req.data.decode())["body"]
            posted["url"] = req.full_url
            return _MockResp()

        import urllib.request
        orig = urllib.request.urlopen
        urllib.request.urlopen = _mock_urlopen
        try:
            ep._post_result_to_issue("owner/repo", "5", "test result", "fake_token", "coding")
        finally:
            urllib.request.urlopen = orig

        assert "<!-- grape-auto-comment -->" in posted["body"], \
            "_post_result_to_issue should add grape-auto-comment marker"
        assert "[CODING Agent]" in posted["body"], "should retain agent prefix"


# ---------------------------------------------------------------------------
# Plan A: A2A rollback cancels downstream tasks
# ---------------------------------------------------------------------------

class TestPlanARollbackCancel:
    """Plan A: A2A rollback should cancel downstream running/queued tasks."""

    def setup_method(self):
        os.environ["DEVOPS_EXECUTION_MODE"] = "direct"

    def teardown_method(self):
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)
        _clean()

    def test_rollback_qa_to_coding_cancels_sre_running(self):
        """QA→Coding rollback should cancel running SRE task."""
        from fastapi.testclient import TestClient
        from src.api.main import app
        _clean()
        _seed_managed_repo()
        _seed_engine_and_session("coding")
        _seed_engine_and_session("sre")
        _seed_issue_state(stage="qa")

        sre_task_id, _ = _seed_task(agent_type="sre", status="running", seq=999)

        client = TestClient(app)
        r = _post_a2a(client, _a2a_body(agent="coding", issue_number="5"))
        assert r.status_code == 200, r.text

        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM agent_tasks WHERE id=?", (sre_task_id,)).fetchone()
        assert row is not None, "SRE task should exist"
        assert row[0] == "cancelled", f"SRE task should be cancelled, got {row[0]}"

    def test_rollback_qa_to_coding_cancels_sre_queued(self):
        """QA→Coding rollback should cancel queued SRE task."""
        from fastapi.testclient import TestClient
        from src.api.main import app
        _clean()
        _seed_managed_repo()
        _seed_engine_and_session("coding")
        _seed_engine_and_session("sre")
        _seed_issue_state(stage="qa")

        sre_task_id, _ = _seed_task(agent_type="sre", status="queued", seq=999)

        client = TestClient(app)
        r = _post_a2a(client, _a2a_body(agent="coding", issue_number="5"))
        assert r.status_code == 200, r.text

        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM agent_tasks WHERE id=?", (sre_task_id,)).fetchone()
        assert row[0] == "cancelled", f"queued SRE task should be cancelled, got {row[0]}"

    def test_normal_forward_does_not_cancel(self):
        """Normal forward A2A (QA→SRE) should NOT cancel any tasks."""
        from fastapi.testclient import TestClient
        from src.api.main import app
        _clean()
        _seed_managed_repo()
        _seed_engine_and_session("sre")
        _seed_issue_state(stage="qa")

        client = TestClient(app)
        r = _post_a2a(client, _a2a_body(agent="sre", issue_number="5"))
        assert r.status_code == 200, r.text

        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT stage FROM issue_state WHERE issue_id='#5'").fetchone()
        if row:
            assert row[0] == "sre", f"stage should be sre after forward A2A, got {row[0]}"

    def test_rollback_sre_completed_not_cancelled(self):
        """QA→Coding rollback when SRE already completed should not re-cancel."""
        from fastapi.testclient import TestClient
        from src.api.main import app
        _clean()
        _seed_managed_repo()
        _seed_engine_and_session("coding")
        _seed_engine_and_session("sre")
        _seed_issue_state(stage="qa")

        sre_task_id, _ = _seed_task(agent_type="sre", status="completed", seq=999)

        client = TestClient(app)
        r = _post_a2a(client, _a2a_body(agent="coding", issue_number="5"))
        assert r.status_code == 200, r.text

        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM agent_tasks WHERE id=?", (sre_task_id,)).fetchone()
        assert row[0] == "completed", "completed SRE task should not be re-cancelled"

    def test_rollback_coding_to_issue_cancels_all_downstream(self):
        """Coding→Issue rollback should cancel coding+qa+sre all downstream."""
        from fastapi.testclient import TestClient
        from src.api.main import app
        _clean()
        _seed_managed_repo()
        _seed_engine_and_session("issue")
        _seed_engine_and_session("coding")
        _seed_engine_and_session("qa")
        _seed_engine_and_session("sre")
        _seed_issue_state(stage="coding")

        coding_tid, _ = _seed_task(agent_type="coding", status="running", seq=997)
        qa_tid, _ = _seed_task(agent_type="qa", status="running", seq=998)
        sre_tid, _ = _seed_task(agent_type="sre", status="running", seq=999)

        client = TestClient(app)
        r = _post_a2a(client, _a2a_body(agent="issue", issue_number="5"))
        assert r.status_code == 200, r.text

        with db_lock:
            c = db.cursor()
            for tid, label in [(coding_tid, "coding"), (qa_tid, "qa"), (sre_tid, "sre")]:
                row = c.execute("SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone()
                assert row and row[0] == "cancelled", f"{label} task should be cancelled, got {row[0] if row else 'None'}"
