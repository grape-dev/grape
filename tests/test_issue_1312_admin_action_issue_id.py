"""Issue #1312: admin_action audit events must inherit real ownership.

Acceptance coverage:
- positive: audit event for target event #30 stores issue_id '#30'
- negative: bare '#123' in title/result/summary is never used, unresolved -> ''
- aggregation: admin_action and '#0' rows are excluded from pipeline window/stats
"""
import contextlib
import os
import sqlite3
import sys

os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

import pytest

from src.session_manager import db, db_lock

REPO = "gitcode:test1312/repo"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test1312/%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test1312/%'")
        db.commit()


def _event_row(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT issue_id, payload FROM events WHERE seq=?", (seq,))
        return c.fetchone()


class TestAuditInheritsOwnership:
    def test_audit_inherits_target_issue(self):
        from src.api.event_store import append_event
        from src.api.routes.events import _audit_admin_action

        target_seq = append_event(REPO, "issue_comment", title="real issue event",
                                  issue_id="#30", agent_type="issue")
        assert target_seq > 0

        _audit_admin_action(REPO, target_seq, "redispatch",
                            {"username": "ops"}, )

        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT issue_id, payload FROM events WHERE repo_key=? AND event_type='admin_action' "
                "AND json_extract(payload, '$.target_event_seq')=? ORDER BY seq DESC LIMIT 1",
                (REPO, target_seq))
            issue_id, payload = c.fetchone()
        import json
        p = json.loads(payload)
        assert issue_id == "#30", f"审计事件应继承目标 Issue，实际 {issue_id!r}"
        assert p["target_event_seq"] == target_seq
        assert p["related"] is True

    def test_audit_missing_target_has_no_ownership(self):
        from src.api.routes.events import _audit_admin_action

        _audit_admin_action(REPO, 999999, "terminate", {"username": "ops"})

        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT issue_id, payload FROM events WHERE repo_key=? AND event_type='admin_action' "
                "AND json_extract(payload, '$.target_event_seq')=999999 ORDER BY seq DESC LIMIT 1",
                (REPO,))
            issue_id, payload = c.fetchone()
        import json
        assert issue_id == "", "目标事件不存在时不得回退成 #target_seq"
        assert json.loads(payload)["related"] is False


class TestAppendEventStructuredOnly:
    def test_bare_hash_title_not_trusted(self):
        from src.api.event_store import append_event

        seq = append_event(REPO, "custom", title="Admin redispatch: event #123")
        assert seq > 0
        assert _event_row(seq)[0] == ""

    def test_bare_hash_result_not_trusted(self):
        from src.api.event_store import append_event

        seq = append_event(REPO, "custom", title="no number", result="fixed #123")
        assert _event_row(seq)[0] == ""

    def test_structured_issue_number_still_trusted(self):
        from src.api.event_store import append_event

        seq = append_event(REPO, "custom", title="no number",
                           payload={"issue_number": 77})
        assert _event_row(seq)[0] == "#77"

    def test_url_link_still_trusted(self):
        from src.api.event_store import append_event

        seq = append_event(REPO, "custom", title="see https://gitcode.com/o/r/issues/42")
        assert _event_row(seq)[0] == "#42"


_SCHEMA = [
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, issue_id TEXT, event_type TEXT,
        agent_type TEXT, title TEXT, summary TEXT, status TEXT, created_at TEXT,
        actor TEXT, duration INTEGER, repo_key TEXT, scene_id TEXT)""",
    """CREATE TABLE issue (
        repo_key TEXT, issue_number INTEGER, title TEXT, status TEXT, stage TEXT,
        owner_agent TEXT, assignee TEXT, scene_id TEXT, parent_issue_id TEXT,
        is_subtask_mode INTEGER, user_id TEXT)""",
    """CREATE TABLE agent_tasks (repo_key TEXT, issue_id TEXT, status TEXT)""",
]


def _seed_pipeline_db():
    conn = sqlite3.connect(":memory:")
    for ddl in _SCHEMA:
        conn.execute(ddl)
    conn.execute(
        "INSERT INTO issue VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (REPO, 30, "real issue", "active", "vod", "vod", "u1", "", None, 0, "u1"))
    rows = [
        ("#30", "issue_opened", "vod", "real issue", "completed", "2026-11-06 10:00:00"),
        ("#55388", "admin_action", "", "Admin redispatch: event #55388", "completed", "2026-11-06 10:01:00"),
        ("#0", "pull_request_merged", "sre", "PR merged", "completed", "2026-11-06 10:02:00"),
    ]
    for iid, et, at, title, st, ts in rows:
        conn.execute(
            "INSERT INTO events (issue_id, event_type, agent_type, title, summary, status, "
            "created_at, actor, duration, repo_key, scene_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (iid, et, at, title, "", st, ts, "tester", 1, REPO, ""))
    conn.commit()
    return conn


def _patch_pipeline(monkeypatch, conn):
    import src.session_manager as sm
    monkeypatch.setattr(sm, "db", conn)
    monkeypatch.setattr(sm, "db_lock", contextlib.nullcontext())
    monkeypatch.setattr("src.api.deps.check_repo_access", lambda *a, **k: None)
    monkeypatch.setattr("src.api.pipeline_config.get_pipeline_config", lambda *a, **k: None)


def test_pipeline_window_excludes_audit_and_unrelated(monkeypatch):
    from src.api.routes import pipeline as pipeline_route

    _patch_pipeline(monkeypatch, _seed_pipeline_db())

    ordered, total, failed = pipeline_route._pipeline_window([REPO], "", 50)
    assert ordered == ["#30"]
    assert total == 1
    assert failed == set()


def test_get_pipeline_stats_exclude_audit_and_unrelated(monkeypatch):
    from src.api.routes import pipeline as pipeline_route

    _patch_pipeline(monkeypatch, _seed_pipeline_db())
    out = pipeline_route.get_pipeline("test1312", "repo", history_limit=50,
                                      history_offset=0, scene_id="", user={"id": "u1"})

    ids = []
    if out.get("active"):
        ids.append(out["active"]["issue_id"])
    ids.extend(h.get("issue_id") for h in out.get("history") or [])
    assert ids == ["#30"]
    assert out["history_total"] == 0
    assert out["today_count"] == 1
    assert out["failed_count"] == 0

class TestBackfillScript:
    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, repo_key TEXT, issue_id TEXT, "
                     "event_type TEXT, payload TEXT)")
        conn.execute("CREATE TABLE issue (repo_key TEXT, issue_number INTEGER)")
        conn.execute("INSERT INTO issue VALUES (?, ?)", (REPO, 30))
        conn.execute("INSERT INTO events VALUES (1, ?, '#30', 'issue_comment', '{}')", (REPO,))
        conn.execute("INSERT INTO events VALUES (2, 'gitcode:old/repo', '#55388', 'admin_action', ?)",
                     ('{"target_event_seq": 1}',))
        conn.execute("INSERT INTO events VALUES (3, ?, '#0', 'admin_action', '{}')", (REPO,))
        conn.commit()
        return conn

    def test_plan_apply_and_idempotent(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        import backfill_admin_action_issue_id as bf

        conn = self._db()
        c = conn.cursor()
        changes = bf._plan_backfill(c)
        assert len(changes) == 1
        assert changes[0]["new_repo_key"] == REPO
        assert changes[0]["new_issue_id"] == "#30"
        assert bf._apply_changes(c, conn, changes) == 1
        c.execute("SELECT repo_key, issue_id FROM events WHERE seq=2")
        assert c.fetchone() == (REPO, "#30")
        assert bf._plan_backfill(c) == []

    def test_normalize_and_rollback(self, tmp_path):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        import backfill_admin_action_issue_id as bf

        conn = self._db()
        c = conn.cursor()
        backup = str(tmp_path / "backup.json")
        total, done, path = bf._normalize_unrelated(c, conn, backup)
        assert (total, done) == (1, 1)
        c.execute("SELECT COUNT(*) FROM events WHERE issue_id='#0'")
        assert c.fetchone()[0] == 0
        assert os.path.exists(path)
        assert bf._rollback(c, conn, path) == 1
        c.execute("SELECT issue_id FROM events WHERE seq=3")
        assert c.fetchone()[0] == "#0"
