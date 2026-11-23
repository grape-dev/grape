"""Issue #3928：注册/心跳上报后端能力，缺 CLI 立即不可用并告警；恢复自动放行。"""
import asyncio
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
        asyncio.run(runtime_heartbeat(HeartbeatBody(runtime_ids=[rid], probe_results=obs),
                                      FakeRequest(), credential_id=cid))
        healthy, _installed, bstatus, _reason, until = _row(rid)
        assert healthy == 0 and bstatus == "unavailable" and until

        ok = {name: {"status": "ok", "latency": 1.0,
                     "last_probe": "2026-11-23T10:10:01+00:00",
                     "error": "", "backend_status": "available"}}
        asyncio.run(runtime_heartbeat(HeartbeatBody(runtime_ids=[rid], probe_results=ok),
                                      FakeRequest(), credential_id=cid))
        healthy, _installed, bstatus, _reason, until = _row(rid)
        assert healthy == 1 and bstatus == "available" and until == ""
    finally:
        _cleanup(cid)


def test_reregister_payload_includes_uninstalled_engine_for_marking(monkeypatch):
    """根因回归：CLI 丢失后 must 仍把 installed=false 条目发给服务端标记 unavailable。"""
    from src.domains.runtime.engine import agent as ra

    class _FakeClient:
        last_register_detail = None

        async def reset_session(self):
            return None

        async def register(self, runtimes, **_kw):
            self.payload = runtimes
            return [], ""

    client = _FakeClient()
    monkeypatch.delenv("CLOUD_WORKER_PROVIDER", raising=False)
    monkeypatch.setattr(ra, "_discover_local_runtimes", lambda: [{
        "name": "codearts", "installed": False, "healthy": False, "cli_path": "",
        "version": "", "backend_capability": {
            "status": "unavailable", "reason": "codearts CLI not found in PATH"},
    }])
    out = asyncio.run(ra._reregister(
        client, prev_installed=[{"name": "codearts", "installed": True}],
        device_name="d-3928"))
    assert out == ([], {}, []), "无可用引擎仍是正常状态，不放进心跳列表"
    assert client.payload and client.payload[0]["name"] == "codearts"
    assert client.payload[0]["installed"] is False
    assert client.payload[0]["backend_capability"]["status"] == "unavailable"


def test_reregister_keeps_snapshot_when_scan_returns_only_uninstalled(monkeypatch):
    """QA 复审 Optional：多引擎主机瞬时全 installed=false 不得把整机 runtime 摘除。"""
    from src.domains.runtime.engine import agent as ra

    class _FakeClient:
        payload = None

        async def reset_session(self):
            return None

        async def register(self, runtimes, **_kw):
            self.payload = runtimes
            return [], ""

    client = _FakeClient()
    monkeypatch.delenv("CLOUD_WORKER_PROVIDER", raising=False)
    monkeypatch.setattr(ra, "_discover_local_runtimes", lambda: [
        {"name": "codearts", "installed": False, "healthy": False, "cli_path": "",
         "version": "", "backend_capability": {"status": "unavailable", "reason": "not found"}},
        {"name": "opencode", "installed": False, "healthy": False, "cli_path": "",
         "version": "", "backend_capability": {"status": "unavailable", "reason": "not found"}},
    ])
    out = asyncio.run(ra._reregister(
        client, prev_installed=[{"name": "codearts", "installed": True},
                                {"name": "opencode", "installed": True}],
        device_name="d-3928"))
    assert out is None, "瞬时全未安装不得把整机 runtime 摘除"
    assert client.payload is None, "沿用快照时不应发起注册"


def test_reregister_still_reports_single_engine_cli_removed(monkeypatch):
    """单引擎主机唯一 CLI 被移除仍需上报（17641 根因链路，不能当扫描抖动放过）。"""
    from src.domains.runtime.engine import agent as ra

    class _FakeClient:
        payload = None

        async def reset_session(self):
            return None

        async def register(self, runtimes, **_kw):
            self.payload = runtimes
            return [], ""

    client = _FakeClient()
    monkeypatch.delenv("CLOUD_WORKER_PROVIDER", raising=False)
    monkeypatch.setattr(ra, "_discover_local_runtimes", lambda: [
        {"name": "codearts", "installed": False, "healthy": False, "cli_path": "",
         "version": "", "backend_capability": {"status": "unavailable", "reason": "not found"}},
    ])
    out = asyncio.run(ra._reregister(
        client, prev_installed=[{"name": "codearts", "installed": True}],
        device_name="d-3928"))
    assert out == ([], {}, [])
    assert client.payload and client.payload[0]["installed"] is False
    assert client.payload[0]["backend_capability"]["status"] == "unavailable"
