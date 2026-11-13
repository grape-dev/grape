"""Issue #1962：a2a_dispatch_guard 时间口径修正与补发自排除通道测试。

覆盖验收标准：
- mysql / sqlite 双后端时区：MySQL CST 墙钟按 +08 解释转 UTC，SQLite 视为 UTC；
- completed a2a_event 三档窗口：120s 内拦截，2h / 9h 放行；
- 手动 a2a-redo 自排除源事件（exclude_event_seq），异源仍拦；下游 running 保护仍在；
- storm event_skipped 120s / 600s 两档计数与真实时间一致。
"""
import asyncio
from datetime import datetime, timezone

import pytest

from src.kernel.session_manager import db, db_lock

REPO = "gitcode:owner/repo1962"
ISSUE = "1962"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM event_skipped WHERE repo_key=?", (REPO,))
        db.commit()


def _seed_a2a(agent="coding", status="completed", offset_seconds=0):
    from src.api.event_store import append_event
    from src.api.time_utils import db_now_str
    seq = append_event(
        repo_key=REPO, event_type="a2a_event",
        title=f"A2A: {agent}", issue_id=f"#{ISSUE}", source="a2a",
        agent_type=agent, summary=f"A2A: → {agent}",
        payload={"issue_number": str(ISSUE), "context": f"seed-{agent}"},
        status=status)
    with db_lock:
        db.cursor().execute(
            "UPDATE events SET created_at=? WHERE seq=?",
            (db_now_str(-offset_seconds), seq))
        db.commit()
    return seq


def _seed_skip(detail="target=coding", offset_seconds=0):
    from src.api.a2a_dispatch_guard import record_skip
    from src.api.time_utils import db_now_str
    record_skip(REPO, ISSUE, "coding", "duplicate_key:1", detail=detail)
    with db_lock:
        c = db.cursor()
        _id = c.execute(
            "SELECT MAX(id) FROM event_skipped WHERE repo_key=?", (REPO,)).fetchone()[0]
        c.execute("UPDATE event_skipped SET created_at=? WHERE id=?",
                  (db_now_str(-offset_seconds), _id))
        db.commit()


class TestParseTsBackend:
    def test_mysql_cst_wallclock_to_utc(self, monkeypatch):
        from src.api import a2a_dispatch_guard as guard
        monkeypatch.setattr("src.api.time_utils._db_backend", lambda: "mysql")
        assert guard._parse_ts("2026-11-11 10:09:37") == datetime(
            2026, 11, 11, 2, 9, 37, tzinfo=timezone.utc)

    def test_sqlite_wallclock_kept_utc(self, monkeypatch):
        from src.api import a2a_dispatch_guard as guard
        monkeypatch.setattr("src.api.time_utils._db_backend", lambda: "sqlite")
        assert guard._parse_ts("2026-11-11 10:09:37") == datetime(
            2026, 11, 11, 10, 9, 37, tzinfo=timezone.utc)

    def test_aware_input_not_double_shifted(self):
        from src.api.a2a_dispatch_guard import _parse_ts
        assert _parse_ts("2026-11-11T10:09:37+08:00") == datetime(
            2026, 11, 11, 2, 9, 37, tzinfo=timezone.utc)
        assert _parse_ts("2026-11-11T02:09:37+00:00") == datetime(
            2026, 11, 11, 2, 9, 37, tzinfo=timezone.utc)

    def test_bad_value_returns_none(self):
        from src.api.a2a_dispatch_guard import _parse_ts
        assert _parse_ts("not-a-time") is None
        assert _parse_ts("") is None
        assert _parse_ts(None) is None


class TestWindowDuplicateWindows:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_within_120s_intercepted(self):
        from src.api.a2a_dispatch_guard import find_window_duplicate
        seq = _seed_a2a(offset_seconds=60)
        hit = find_window_duplicate(REPO, ISSUE, "coding", "default", 120)
        assert hit.get("seq") == seq, hit

    def test_two_hours_passes(self):
        from src.api.a2a_dispatch_guard import find_window_duplicate
        _seed_a2a(offset_seconds=7200)
        assert find_window_duplicate(REPO, ISSUE, "coding", "default", 120) == {}

    def test_nine_hours_passes(self):
        from src.api.a2a_dispatch_guard import find_window_duplicate
        _seed_a2a(offset_seconds=32400)
        assert find_window_duplicate(REPO, ISSUE, "coding", "default", 120) == {}

    def test_exclude_seq_skips_only_source_event(self):
        from src.api.a2a_dispatch_guard import find_window_duplicate
        source = _seed_a2a(offset_seconds=30)
        assert find_window_duplicate(
            REPO, ISSUE, "coding", "default", 120, exclude_seq=source) == {}
        other = _seed_a2a(offset_seconds=20)
        hit = find_window_duplicate(
            REPO, ISSUE, "coding", "default", 120, exclude_seq=source)
        assert hit.get("seq") == other, hit

    def test_active_exclude_seq(self):
        from src.api.a2a_dispatch_guard import find_active_duplicate
        seed = _seed_a2a(status="running", offset_seconds=10)
        assert find_active_duplicate(
            REPO, ISSUE, "coding", "default", "ctx-other", 120,
            exclude_seq=seed) == {}


class TestRedoSelfExclude:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def _run(self, seq, agent, exclude_event_seq=0):
        from src.domains.runtime.routes.events import a2a_redo_event, A2ARedoBody
        user = {"id": 1, "username": "tester", "auth_type": "session_token"}
        return asyncio.run(
            a2a_redo_event(seq, A2ARedoBody(
                agent=agent, exclude_event_seq=exclude_event_seq), user))

    def _patch_endpoint(self, monkeypatch, captured):
        async def _fake(request, body, user):
            captured["payload"] = dict(body.payload or {})
            return {"seq": 999}
        monkeypatch.setattr("src.domains.runtime.routes.webhook.a2a_endpoint", _fake)

    def test_redo_passes_source_seq_by_default(self, monkeypatch):
        from src.api.event_store import append_event
        seq = append_event(
            repo_key=REPO, event_type="a2a_event", title="A2A: coding",
            issue_id=f"#{ISSUE}", source="a2a", agent_type="coding",
            summary="A2A: → coding", payload={"context": "src"}, status="completed")
        captured = {}
        self._patch_endpoint(monkeypatch, captured)
        res = self._run(seq, "coding")
        assert res.get("seq") == 999
        assert captured["payload"]["exclude_event_seq"] == seq

    def test_explicit_exclude_seq_takes_priority(self, monkeypatch):
        from src.api.event_store import append_event
        seq = append_event(
            repo_key=REPO, event_type="a2a_event", title="A2A: coding",
            issue_id=f"#{ISSUE}", source="a2a", agent_type="coding",
            summary="A2A: → coding", payload={"context": "src"}, status="completed")
        captured = {}
        self._patch_endpoint(monkeypatch, captured)
        self._run(seq, "coding", exclude_event_seq=4321)
        assert captured["payload"]["exclude_event_seq"] == 4321

    def test_downstream_running_still_blocks_redo(self):
        from src.api.a2a_dispatch_guard import find_running_downstream
        _seed_a2a(agent="coding", status="completed", offset_seconds=30)
        qa_seq = _seed_a2a(agent="qa", status="running", offset_seconds=5)
        nodes = [{"id": "coding", "next": "qa"}, {"id": "qa", "next": ""}]
        hit = find_running_downstream(REPO, ISSUE, "coding", "default", nodes)
        assert hit.get("seq") == qa_seq, hit


class TestAppendGuardedExclude:
    """QA 阻断项回归：原子复查（append_guarded_a2a_event）必须透传 exclude_seq。"""

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def _append(self, exclude_seq=0):
        from src.api.a2a_dispatch_guard import append_guarded_a2a_event
        return append_guarded_a2a_event(
            REPO, ISSUE, "coding", "default",
            title="A2A: coding", summary="A2A: → coding",
            payload={"issue_number": str(ISSUE), "context": "redo"},
            actor="tester", status="running", context_key="redo",
            idempotency_key="redo-key", exclude_seq=exclude_seq)

    def test_atomic_recheck_excludes_source_event(self):
        source = _seed_a2a(status="completed", offset_seconds=30)
        kind, value = self._append(exclude_seq=source)
        assert kind == "ok", (kind, value)
        assert isinstance(value, int) and value != source

    def test_atomic_recheck_without_exclude_still_blocks(self):
        source = _seed_a2a(status="completed", offset_seconds=30)
        kind, value = self._append()
        assert kind == "duplicate_key", (kind, value)
        assert value.get("seq") == source, value


class TestRedoEndToEnd:
    """同 Issue 同 agent 手动补发端到端放行（排除源事件），且下游 running 保护仍在。"""

    def setup_method(self):
        _clean()
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                    "VALUES (?, 'owner', 'repo1962', 'owner/repo1962')", (REPO,))
            except Exception:
                pass
            db.commit()

    def teardown_method(self):
        _clean()
        with db_lock:
            c = db.cursor()
            try:
                c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
            except Exception:
                pass
            db.commit()

    def _post(self, exclude_seq):
        import os
        from fastapi.testclient import TestClient
        from src.api.main import app
        os.environ["DEVOPS_EXECUTION_MODE"] = "direct"
        payload = {
            "repository": "owner/repo1962",
            "account": "owner/repo1962",
            "issue_number": ISSUE,
            "pr_number": "",
            "context": "手动补发 a2a",
        }
        if exclude_seq:
            payload["exclude_event_seq"] = exclude_seq
        return TestClient(app).post(
            "/api/a2a", json={"agent": "coding", "payload": payload})

    def test_redo_source_event_self_excluded_end_to_end(self):
        import os
        try:
            source = _seed_a2a(status="completed", offset_seconds=30)
            r = self._post(exclude_seq=source)
            assert r.status_code == 200, r.text
            assert r.json().get("status") == "ok", r.text
            assert r.json().get("seq") != source, r.text
        finally:
            os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def test_without_exclude_source_event_blocks_end_to_end(self):
        import os
        try:
            source = _seed_a2a(status="completed", offset_seconds=30)
            r = self._post(exclude_seq=0)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("skipped") == "duplicate_key", r.text
            assert str(body.get("reason") or "").startswith("duplicate_key"), r.text
            assert (body.get("original_event") or {}).get("seq") == source, r.text
        finally:
            os.environ.pop("DEVOPS_EXECUTION_MODE", None)


class TestStormBackendConsistency:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_storm_120s_and_600s_counts(self):
        from src.api.a2a_dispatch_guard import detect_dispatch_storm
        _seed_a2a(offset_seconds=60)
        _seed_a2a(offset_seconds=300)
        _seed_skip(offset_seconds=60)
        _seed_skip(offset_seconds=300)

        short = detect_dispatch_storm(REPO, ISSUE, "coding", upcoming=0, window_seconds=120)
        assert short["count"] == 2, short
        long = detect_dispatch_storm(REPO, ISSUE, "coding", upcoming=0, window_seconds=600)
        assert long["count"] == 4, long

    def test_skip_outside_window_not_counted(self):
        from src.api.a2a_dispatch_guard import detect_dispatch_storm
        _seed_skip(offset_seconds=1800)
        result = detect_dispatch_storm(REPO, ISSUE, "coding", upcoming=0, window_seconds=600)
        assert result["count"] == 0, result