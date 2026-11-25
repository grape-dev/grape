"""Issue #4170：交付终态（PR merged + Issue closed）后 issue_comment 终态抑制。

验收：
- 终态 Issue 新评论 0 派发、0 复位，事件仍可查（审计不丢）；
- 非终态 Issue 的派发与复位行为回归不变；
- 终态判定要求 closed + merged 双条件（reopen 后自然脱离终态）。
"""
import json
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.db_setup import init_database
from src.kernel.session_manager import db, db_lock

RK = "gitcode:test/i4170"
NUM = 4171
ISSUE_ID = f"#{NUM}"


def setup_module():
    init_database()


class _FakeVcsClient:
    """替身 VcsApiClient：只回答 Issue 仓库态（closed/open/unknown）。"""

    def __init__(self, state):
        self._state = state

    def get_issue_state(self, issue_number):
        return self._state


def _fake_vcs(state):
    return lambda repo_key: _FakeVcsClient(state)


def _clean():
    with db_lock:
        c = db.cursor()
        for table in ("events", "event_skipped", "issue", "agent_tasks",
                      "event_active_guard"):
            try:
                c.execute(f"DELETE FROM {table} WHERE repo_key=?", (RK,))
            except Exception:
                pass
        db.commit()


def _seed_issue(status="pending_review", stage="pending_review", owner="qa"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (RK, NUM))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "task_description, status, stage, owner_agent) VALUES (?,?,?,?,?,?,?,?,?)",
            ("0", "", RK, NUM, "终态抑制需求", "d", status, stage, owner))
        db.commit()


def _seed_merged_event(issue_id=ISSUE_ID):
    from src.api.event_store import append_event
    append_event(
        RK, "pull_request_merged", title="merge carrier PR", issue_id=issue_id,
        agent_type="sre", source="gitcode", status="completed",
        payload={"issue_number": NUM})


def _issue_row():
    with db_lock:
        return db.cursor().execute(
            "SELECT status, stage, owner_agent FROM issue "
            "WHERE repo_key=? AND issue_number=?", (RK, NUM)).fetchone()


def _comment_payload(note="再确认一下验收结论", cid=4170001):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i4170"},
        "object_attributes": {"noteable_type": "Issue", "iid": NUM,
                              "note": note, "id": cid},
        "issue": {"iid": NUM, "number": NUM},
        "user": {"username": "realuser4170"},
    }


def _run_comment(payload=None):
    from src.domains.runtime.pipeline.event_pipeline import run_pipeline
    raw = json.dumps(payload or _comment_payload()).encode()
    headers = {"X-GitCode-Event": "Note Hook"}
    return run_pipeline(raw, headers, account="test/i4170",
                        repo_key_override="test/i4170")


def _issue_comment_rows():
    with db_lock:
        return db.cursor().execute(
            "SELECT seq, status, agent_type, payload FROM events "
            "WHERE repo_key=? AND issue_id=? AND event_type='issue_comment' "
            "ORDER BY seq DESC", (RK, ISSUE_ID)).fetchall()


def _agent_task_count():
    with db_lock:
        return db.cursor().execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE repo_key=?", (RK,)).fetchone()[0]


class TestTerminalPredicate:
    def test_merged_and_closed_is_terminal(self):
        _clean()
        _seed_issue()
        _seed_merged_event()
        from src.api.issues_center import is_issue_delivery_terminal
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("closed")):
            assert is_issue_delivery_terminal(RK, ISSUE_ID) is True

    def test_merged_but_open_not_terminal(self):
        _clean()
        _seed_issue()
        _seed_merged_event()
        from src.api.issues_center import is_issue_delivery_terminal
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("open")):
            assert is_issue_delivery_terminal(RK, ISSUE_ID) is False

    def test_closed_without_merged_pr_not_terminal(self):
        _clean()
        _seed_issue()
        from src.api.issues_center import is_issue_delivery_terminal
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("closed")):
            assert is_issue_delivery_terminal(RK, ISSUE_ID) is False

    def test_merged_event_with_bare_issue_id_still_detected(self):
        _clean()
        _seed_issue()
        _seed_merged_event(issue_id=str(NUM))
        from src.api.issues_center import is_issue_delivery_terminal
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("closed")):
            assert is_issue_delivery_terminal(RK, ISSUE_ID) is True

    def test_vcs_unknown_fails_open(self):
        _clean()
        _seed_issue()
        _seed_merged_event()
        from src.api.issues_center import is_issue_delivery_terminal
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("")):
            assert is_issue_delivery_terminal(RK, ISSUE_ID) is False


class TestTerminalCommentDispatchGuard:
    def test_terminal_comment_recorded_not_dispatched(self):
        _clean()
        _seed_issue()
        _seed_merged_event()
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("closed")):
            res = _run_comment()
        assert res.get("skipped") == "terminal_delivery_suppressed"
        # 0 派发：无 agent_tasks
        assert _agent_task_count() == 0
        # 事件仍可查：一行 status=skipped 的审计事件，payload 带抑制标记
        rows = _issue_comment_rows()
        assert len(rows) == 1, rows
        assert rows[0][1] == "skipped"
        assert "_terminal_suppressed" in (rows[0][3] or "")
        # 0 复位：stage/task_status 原样保留
        assert _issue_row() == ("pending_review", "pending_review", "qa")

    def test_duplicate_terminal_comment_keeps_single_audit_row(self):
        _clean()
        _seed_issue()
        _seed_merged_event()
        payload = _comment_payload(cid=4170099)
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("closed")):
            r1 = _run_comment(payload)
            r2 = _run_comment(payload)
        assert r1.get("skipped") == "terminal_delivery_suppressed"
        assert r2.get("skipped") == "terminal_delivery_suppressed"
        # 同评论 id 幂等：只保留一条审计行
        assert len(_issue_comment_rows()) == 1

    def test_non_terminal_comment_not_terminal_suppressed(self):
        _clean()
        _seed_issue(status="processing", stage="coding", owner="coding")
        _seed_merged_event()
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("open")):
            res = _run_comment()
        assert res.get("skipped") != "terminal_delivery_suppressed"
        assert not any("_terminal_suppressed" in (r[3] or "") for r in _issue_comment_rows())
        # 非终态：stage/task_status 不被终态守卫改写
        assert _issue_row() == ("processing", "coding", "coding")


class TestTerminalResumeSuppression:
    def test_terminal_pending_review_not_reset(self):
        _clean()
        _seed_issue(status="pending_review", stage="pending_review", owner="qa")
        _seed_merged_event()
        from src.api.issues_center import _resume_issue_waiting
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("closed")):
            assert _resume_issue_waiting(RK, NUM) is False
        assert _issue_row() == ("pending_review", "pending_review", "qa")

    def test_non_terminal_pending_review_still_resumes(self):
        _clean()
        _seed_issue(status="pending_review", stage="pending_review", owner="qa")
        _seed_merged_event()
        from src.api.issues_center import _resume_issue_waiting
        with patch("src.domains.runtime.issues_center._pr_vcs_client", _fake_vcs("open")):
            assert _resume_issue_waiting(RK, NUM) is True
        row = _issue_row()
        assert row[0] == "processing"