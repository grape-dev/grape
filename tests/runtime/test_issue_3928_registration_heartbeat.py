"""Issue #3928：注册/心跳上报后端能力，缺 CLI 立即不可用并告警；恢复自动放行。"""
import logging
import uuid
from datetime import datetime, timezone

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.routes.runtime import (
    HeartbeatBody, RuntimeRegBody, runtime_heartbeat, runtime_register)
from probe_state_support import FakeRequest, insert_credential, insert_runtime


def _insert_runtime_row(cid, name, healthy=1, probe_status="ok"):
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed, "
            "scope, last_seen_at, probe_status, worker_version) "
            "VALUES (?, ?, 'online', ?, 1, 'global', ?, ?, 'w-3928')",
            (cid, name, healthy, now, probe_status))
        rid = c.lastrowid
        db.commit()
    return rid


def _row(rid):
    with db_lock:
        return db.cursor().execute(
            "SELECT healthy, installed, backend_status, backend_reason, "
            "circuit_open_until FROM agent_runtimes WHERE id=?", (rid,)).fetchone()


def _cleanup(cid):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def test_register_missing_cli_marks_unavailable_and_warns(caplog):
    cid = insert_credential("cred-3928r")
    name = f"codearts-cred0-{uuid.uuid4().hex[:6]}"
    rid = _insert_runtime_row(cid, name)
    try:
        body = RuntimeRegBody(device_name="d-3928", runtimes=[{
            "name": name, "version": "", "healthy": False, "installed": False,
            "cli_path": "",
            "backend_capability": {"status": "unavailable",
                                   "reason": "codearts CLI not found in PATH",
                                   "checked_at": datetime.now(timezone.utc).isoformat()},
        }])
        with caplog.at_level(logging.WARNING, logger="devops-bot"):
            resp = runtime_register(body, FakeRequest(), credential_id=cid)
        assert resp["runtimes"] == []
        healthy, installed, bstatus, reason, until = _row(rid)
        assert healthy == 0 and installed == 0
        assert bstatus == "unavailable" and "codearts" in reason.lower()
        assert until, "缺 CLI 应立即熔断（不进认领候选集）"
        warn = [r.getMessage() for r in caplog.records
                if f"runtime_id={rid}" in r.getMessage()]
        assert warn, "告警必须带 runtime_id"
    finally:
        _cleanup(cid)


def test_register_available_codearts_keeps_healthy_and_clears_circuit():
    cid = insert_credential("cred-3928r")
    name = f"codearts-ok-{uuid.uuid4().hex[:6]}"
    rid = _insert_runtime_row(cid, name)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET backend_status='unavailable', "
                      "circuit_open_until='2099-01-01T00:00:00+00:00' WHERE id=?", (rid,))
            db.commit()
        body = RuntimeRegBody(device_name="d-3928", runtimes=[{
            "name": name, "version": "1.0", "healthy": True, "installed": True,
            "cli_path": "/usr/bin/codearts",
            "backend_capability": {"status": "available", "reason": "",
                                   "checked_at": datetime.now(timezone.utc).isoformat()},
        }])
        runtime_register(body, FakeRequest(), credential_id=cid)
        healthy, installed, bstatus, _reason, until = _row(rid)
        assert healthy == 1 and installed == 1
        assert bstatus == "available" and until == ""
    finally:
        _cleanup(cid)


def test_heartbeat_unavailable_degrades_then_ok_recovers():
    cid = insert_credential("cred-3928h")
    name = f"codearts-hb-{uuid.uuid4().hex[:6]}"
    rid = insert_runtime(cid, name)
    try:
        obs = {name: {"status": "skip", "latency": 0,
                      "last_probe": "2026-11-23T10:00:01+00:00",
                      "error": "not installed",
                      "backend_status": "unavailable"}}
        runtime_heartbeat(HeartbeatBody(runtime_ids=[rid], probe_results=obs),
                          FakeRequest(), credential_id=cid)
        healthy, _installed, bstatus, _reason, until = _row(rid)
        assert healthy == 0 and bstatus == "unavailable" and until

        ok = {name: {"status": "ok", "latency": 1.0,
                     "last_probe": "2026-11-23T10:10:01+00:00",
                     "error": "", "backend_status": "available"}}
        runtime_heartbeat(HeartbeatBody(runtime_ids=[rid], probe_results=ok),
                          FakeRequest(), credential_id=cid)
        healthy, _installed, bstatus, _reason, until = _row(rid)
        assert healthy == 1 and bstatus == "available" and until == ""
    finally:
        _cleanup(cid)