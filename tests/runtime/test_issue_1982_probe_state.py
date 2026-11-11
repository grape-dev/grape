"""Issue #1982：探活三态化与 fd 类 spawn_failed 不触发引擎摘除。"""
import errno
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_credential():
    name = f"cred-1982-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"hash_{name}", name, f"dev-{name}", _now_iso()))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid, name, healthy=1, probe_fail_count=0):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed, scope, last_seen_at, "
            "probe_fail_count, probe_status, probe_last) "
            "VALUES (?, ?, 'online', ?, 1, 'global', ?, ?, 'ok', '')",
            (cid, name, healthy, _now_iso(), probe_fail_count))
        rid = c.lastrowid
        db.commit()
    return rid


def _cleanup(cid):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _state(name, cid):
    with db_lock:
        row = db.cursor().execute(
            "SELECT healthy, probe_status, probe_fail_count FROM agent_runtimes "
            "WHERE name=? AND credential_id=?", (name, cid)).fetchone()
    return tuple(row) if row else None


class _FakeRequest:
    headers = {}
    client = None


def _heartbeat(probe_results, runtime_ids, cid):
    from src.domains.runtime.routes.runtime import runtime_heartbeat, HeartbeatBody
    body = HeartbeatBody(runtime_ids=runtime_ids, probe_results=probe_results)
    return runtime_heartbeat(body, _FakeRequest(), credential_id=cid)


def test_spawn_failed_not_counted_as_consecutive_failure():
    cid = _insert_credential()
    name = f"engine-1982-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=1)
        _heartbeat({name: {"status": "spawn_failed", "latency": 0.1,
                           "last_probe": "2026-11-11T00:00:00+00:00",
                           "error": "OSError: [Errno 24] Too many open files"}}, [rid], cid)
        healthy, status, count = _state(name, cid)
        assert status == "spawn_failed"
        assert count == 1, "spawn_failed 不计入探活连续失败"
        assert healthy == 1, "spawn_failed 不得触发引擎摘除"
    finally:
        _cleanup(cid)


def test_model_unreachable_counted_and_degrades_on_second():
    cid = _insert_credential()
    name = f"engine-1982-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
        _heartbeat({name: {"status": "model_unreachable", "latency": 15.0,
                           "last_probe": "2026-11-11T00:10:00+00:00",
                           "error": "no JSON-RPC response"}}, [rid], cid)
        assert _state(name, cid)[2] == 1
        _heartbeat({name: {"status": "model_unreachable", "latency": 15.0,
                           "last_probe": "2026-11-11T00:20:00+00:00",
                           "error": "no JSON-RPC response"}}, [rid], cid)
        healthy, status, count = _state(name, cid)
        assert status == "model_unreachable"
        assert count == 2
        assert healthy == 0, "模型不可达连续 2 次应正常降级"
    finally:
        _cleanup(cid)


def test_hermes_probe_emfile_classified_as_spawn_failed():
    from src.domains.runtime.engine import agent as ra
    exc = OSError(errno.EMFILE, "Too many open files")
    with patch("src.domains.runtime.engine.agent._subprocess.Popen", side_effect=exc):
        res = ra._probe_hermes_acp("/bin/hermes")
    assert res["status"] == "spawn_failed", res
    assert "Errno 24" in res["error"] or "Too many open files" in res["error"]
