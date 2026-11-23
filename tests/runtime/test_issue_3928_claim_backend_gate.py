"""Issue #3928：认领闸门按后端健康态收紧（正常/unknown 零回归）。"""
import uuid
from datetime import datetime, timezone

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import backend_health, service
from src.domains.runtime.engine.service import _has_verified_executor


def test_failed_probe_blocks_even_with_worker_version():
    assert _has_verified_executor("fail", "2.0.0", credential_id=9,
                                  installed=True, healthy=1) is False
    assert _has_verified_executor("failed", "2.0.0", credential_id=9,
                                  installed=True, healthy=1) is False


def test_backend_unavailable_or_degraded_blocks():
    assert _has_verified_executor(
        "ok", "2.0.0", credential_id=9, installed=True, healthy=1,
        backend_status=backend_health.STATUS_UNAVAILABLE) is False
    assert _has_verified_executor(
        "ok", "2.0.0", credential_id=9, installed=True, healthy=1,
        backend_status=backend_health.STATUS_DEGRADED) is False


def test_unhealthy_blocks_remote():
    assert _has_verified_executor("pending", "2.0.0", credential_id=9,
                                  installed=True, healthy=0) is False


def test_pending_with_worker_version_keeps_legacy_allow():
    assert _has_verified_executor("pending", "2.0.0", credential_id=9,
                                  installed=True, healthy=1) is True


def test_normal_codearts_keeps_claimable():
    assert _has_verified_executor(
        "ok", "2.0.0", credential_id=9, installed=True, healthy=1,
        backend_status=backend_health.STATUS_AVAILABLE) is True


def test_unknown_probe_state_keeps_legacy_behavior_for_remote():
    assert _has_verified_executor("", "", credential_id=9) is True


def _insert_credential():
    name = f"cred-3928-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"h_{name}", name, f"dev-{name}",
             datetime.now(timezone.utc).isoformat()))
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


def _insert_pool_task(tag="all"):
    from src.domains.runtime.engine import service as svc
    return svc.enqueue_task(
        agent_session_id=f"sess-3928-{uuid.uuid4().hex[:8]}",
        runtime_id=0, prompt="p", issue_id="#3928",
        repo_key="gitcode:issue3928/repo", agent_type="qa", skill="qa",
        pool_agent_type="qa", pool_tag=tag)


def _cleanup(cid):
    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:issue3928/%'")
        c.execute(
            "DELETE FROM engine_capabilities WHERE runtime_id IN "
            "(SELECT id FROM agent_runtimes WHERE credential_id=?)", (cid,))
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def test_claim_by_tag_skips_unavailable_runtime_and_other_instance_can_claim():
    cid = _insert_credential()
    bad = _insert_runtime(cid, "codearts-cred0", healthy=0,
                          probe_status="fail", backend_status="unavailable")
    good = _insert_runtime(cid, "codearts-cred1")
    try:
        task_id = _insert_pool_task()
        assert service.claim_by_tag("all", bad) is None, "缺 CLI 实例不得认领"
        task = service.claim_by_tag("all", good)
        assert task and task["id"] == task_id
        assert service.get_task(task_id)["runtime_id"] == good
    finally:
        _cleanup(cid)