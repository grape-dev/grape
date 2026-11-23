"""Issue #3928：后端启动失败自动重派 + runtime 熔断 + 半开试探 + 超限判死。"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import backend_failover, backend_health, service


def _insert_credential():
    name = f"cred-3928f-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"h_{name}", name, f"dev-{name}", datetime.now(timezone.utc).isoformat()))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid, name, healthy=1, probe_status="ok", backend_status="available"):
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed, "
            "scope, last_seen_at, probe_status, worker_version, backend_status) "
            "VALUES (?, ?, 'online', ?, 1, 'global', ?, ?, 'w-3928', ?)",
            (cid, name, healthy, now, probe_status, backend_status))
        rid = c.lastrowid
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled) "
            "VALUES (?, 'all', 5, 50, 1)", (rid,))
        db.commit()
    return rid


def _insert_task(runtime_id, status="running", attempts=0):
    tid = f"t_3928_{uuid.uuid4().hex[:10]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "skill, prompt, status, pool_tag, pool_agent_type, attempts, started_at, created_at) "
            "VALUES (?, ?, ?, 'gitcode:issue3928f/repo', 'qa', 'qa', 'p', ?, 'all', 'qa', ?, ?, ?)",
            (tid, f"sess-{tid}", runtime_id, status, attempts,
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()))
        db.commit()
    return tid


def _runtime_row(rid):
    with db_lock:
        return db.cursor().execute(
            "SELECT healthy, backend_status, backend_reason, circuit_open_until, "
            "circuit_fail_count, circuit_half_open FROM agent_runtimes WHERE id=?",
            (rid,)).fetchone()


def _task_row(tid):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, runtime_id, attempts, errors FROM agent_tasks WHERE id=?",
            (tid,)).fetchone()


def _cleanup(cid):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:issue3928f/%'")
        c.execute(
            "DELETE FROM engine_capabilities WHERE runtime_id IN "
            "(SELECT id FROM agent_runtimes WHERE credential_id=?)", (cid,))
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def test_backend_unavailable_requeues_and_opens_circuit(monkeypatch):
    monkeypatch.setenv("GRAPE_BACKEND_FAILOVER_MAX", "2")
    cid = _insert_credential()
    bad = _insert_runtime(cid, "codearts-cred0", healthy=1, probe_status="ok")
    try:
        tid = _insert_task(bad)
        task = service.get_task(tid)
        out = backend_failover.handle_task_failure(
            tid, task, "engine exit_code=-1, errors: codearts not found", -1)
        assert out == "requeued"
        st, rid, attempts, errors = _task_row(tid)
        assert st == "queued" and rid == 0 and attempts == 1
        assert "BACKEND_UNAVAILABLE_FAILOVER" in (errors or "")
        healthy, bstatus, reason, until, fail_count, _half = _runtime_row(bad)
        assert healthy == 0 and bstatus == "unavailable"
        assert "codearts" in (reason or "").lower()
        assert until and fail_count == 1
        assert not backend_health.claim_allowed(bad), "冷却期内故障实例不得认领"
    finally:
        _cleanup(cid)


def test_requeued_task_is_claimed_by_healthy_instance(monkeypatch):
    monkeypatch.setenv("GRAPE_BACKEND_FAILOVER_MAX", "2")
    cid = _insert_credential()
    bad = _insert_runtime(cid, "codearts-cred0")
    good = _insert_runtime(cid, "codearts-cred1")
    try:
        tid = _insert_task(bad)
        backend_failover.handle_task_failure(
            tid, service.get_task(tid), "codearts CLI not found in PATH", -1)
        assert service.claim_by_tag("all", bad) is None
        task = service.claim_by_tag("all", good)
        assert task and task["id"] == tid, "重派后应由健康同类型实例认领"
        assert service.get_task(tid)["runtime_id"] == good
    finally:
        _cleanup(cid)


def test_failover_exhausted_marks_task_for_failure(monkeypatch):
    monkeypatch.setenv("GRAPE_BACKEND_FAILOVER_MAX", "2")
    cid = _insert_credential()
    bad = _insert_runtime(cid, "codearts-cred0")
    try:
        tid = _insert_task(bad, attempts=2)
        out = backend_failover.handle_task_failure(
            tid, service.get_task(tid), "codearts not found", -1)
        assert out == "exhausted"
        st, rid, attempts, errors = _task_row(tid)
        assert st == "running" and rid == bad
        assert attempts == 2
        assert "BACKEND_UNAVAILABLE_EXHAUSTED" in (errors or "")
        _healthy, bstatus, _reason, _until, fail_count, _half = _runtime_row(bad)
        assert bstatus == "unavailable" and fail_count >= 1
    finally:
        _cleanup(cid)


def test_half_open_allows_only_one_probe_then_recovers(monkeypatch):
    monkeypatch.setenv("GRAPE_BACKEND_FAILOVER_MAX", "2")
    cid = _insert_credential()
    rid = _insert_runtime(cid, "codearts-cred0")
    try:
        backend_health.note_backend_failure(rid, "codearts not found")
        assert not backend_health.claim_allowed(rid)
        # 冷却结束 → 允许一次半开试探
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET circuit_open_until=?, circuit_half_open=0 "
                      "WHERE id=?", (past, rid))
            db.commit()
        assert backend_health.claim_allowed(rid)
        with db_lock:
            c = db.cursor()
            backend_health.note_claim_started(c, rid)
            db.commit()
        assert not backend_health.claim_allowed(rid), "半开试探期间不得再并发认领"
        with db_lock:
            c = db.cursor()
            backend_health.note_claim_outcome(c, rid, backend_failed=False)
            db.commit()
        healthy, bstatus, reason, until, fail_count, half = _runtime_row(rid)
        assert healthy == 1 and bstatus == "available"
        assert until == "" and fail_count == 0 and half == 0
    finally:
        _cleanup(cid)


def test_non_backend_failure_releases_half_open(monkeypatch):
    monkeypatch.setenv("GRAPE_BACKEND_FAILOVER_MAX", "2")
    cid = _insert_credential()
    rid = _insert_runtime(cid, "codearts-cred0", healthy=0, probe_status="fail",
                          backend_status="unavailable")
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET circuit_open_until=? WHERE id=?",
                      ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), rid))
            backend_health.note_claim_started(c, rid)
            db.commit()
        tid = _insert_task(rid)
        out = backend_failover.handle_task_failure(
            tid, service.get_task(tid), "tests failed: 2 assertions", 1)
        assert out == "not_backend"
        healthy, bstatus, _reason, until, fail_count, half = _runtime_row(rid)
        assert healthy == 1 and bstatus == "available"
        assert until == "" and fail_count == 0 and half == 0
    finally:
        _cleanup(cid)