"""Issue #3928：认领闸门按后端健康态收紧（正常/unknown 零回归）。"""
import uuid
from datetime import datetime, timedelta, timezone

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import backend_health, engine_select, service
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

# ── QA 复审 R1：degraded 短冷却 + 半开（不再等 600s 探活） ─────

def _runtime_row(rid):
    with db_lock:
        return db.cursor().execute(
            "SELECT healthy, backend_status, backend_reason, circuit_open_until, "
            "circuit_fail_count, circuit_half_open FROM agent_runtimes WHERE id=?",
            (rid,)).fetchone()


def _expire_circuit(rid):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET circuit_open_until=?, circuit_half_open=0 "
                  "WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                                 rid))
        db.commit()


def _insert_runtime_full(cid, name, backend_status="available", until="", fail_count=0,
                         healthy=1, probe_status="ok"):
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed, "
            "scope, last_seen_at, probe_status, worker_version, backend_status, "
            "circuit_open_until, circuit_fail_count) "
            "VALUES (?, ?, 'online', ?, 1, 'global', ?, ?, 'w-3928', ?, ?, ?)",
            (cid, name, healthy, now, probe_status, backend_status, until, fail_count))
        rid = c.lastrowid
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled) "
            "VALUES (?, 'all', 5, 50, 1)", (rid,))
        db.commit()
    return rid


def test_degraded_opens_short_cooldown_then_half_open_recovers():
    cid = _insert_credential()
    rid = _insert_runtime_full(cid, "codearts-cred0")
    try:
        with db_lock:
            c = db.cursor()
            backend_health.observe_capability(
                c, rid, backend_health.STATUS_DEGRADED, "spawn_failed: EMFILE")
            db.commit()
        healthy, bstatus, reason, until, fail_count, half = _runtime_row(rid)
        assert bstatus == "degraded" and "EMFILE" in (reason or "")
        assert healthy == 1, "degraded 不得改写 #1982 的 healthy 阈值语义"
        assert until, "degraded 必须落冷却窗口，不能再无限期拒领"
        assert int(fail_count or 0) == 1 and half == 0
        until_ts = backend_health._ts(until)
        now_ts = datetime.now(timezone.utc).timestamp()
        assert until_ts is not None and 0 < until_ts - now_ts <= 600, \
            "冷却必须显著短于探活周期（600s），否则仍是可用性回归"
        assert backend_health.claim_allowed(rid) is False, "冷却期内不参与认领"
        _expire_circuit(rid)
        assert backend_health.claim_allowed(rid) is True, "冷却结束放行一次半开试探"
        with db_lock:
            c = db.cursor()
            backend_health.note_claim_started(c, rid)
            db.commit()
        assert backend_health.claim_allowed(rid) is False, "半开试探在飞期间不得并发认领"
        with db_lock:
            c = db.cursor()
            backend_health.note_claim_outcome(c, rid, backend_failed=False, executed=True)
            db.commit()
        healthy, bstatus, _reason, until, fail_count, half = _runtime_row(rid)
        assert bstatus == "available" and healthy == 1
        assert until == "" and fail_count == 0 and half == 0
    finally:
        _cleanup(cid)


def test_degraded_repeat_observation_backs_off_with_cap():
    cid = _insert_credential()
    rid = _insert_runtime_full(cid, "codearts-cred0")
    try:
        spans = []
        for _ in range(3):
            with db_lock:
                c = db.cursor()
                backend_health.observe_capability(
                    c, rid, backend_health.STATUS_DEGRADED, "spawn_failed")
                db.commit()
            _healthy, _bs, _reason, until, fail_count, _half = _runtime_row(rid)
            assert int(fail_count) == len(spans) + 1, "连续 degraded 应累加熔断计数"
            spans.append(backend_health._ts(until))
            _expire_circuit(rid)
        assert spans[0] < spans[1] < spans[2], "冷却应指数退避"
        assert spans[2] - datetime.now(timezone.utc).timestamp() <= 600, "冷却须有上限"
    finally:
        _cleanup(cid)


# ── QA 复审 R2：派发候选 / 池可达性与认领闸门同尺 ─────────────

def test_dispatch_candidates_skip_backend_blocked_engine():
    cid = _insert_credential()
    open_until = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    degraded = _insert_runtime_full(cid, "codearts-cred0", backend_status="degraded",
                                    until=open_until, fail_count=1)
    unavailable = _insert_runtime_full(cid, "codearts-cred1", backend_status="unavailable",
                                       until=open_until, fail_count=1, healthy=0,
                                       probe_status="fail")
    good = _insert_runtime_full(cid, "codearts-cred2")
    try:
        cfg = dict(engine_select._DEFAULT_CFG)
        members = engine_select._engines_by_ids([degraded, unavailable, good], "", cfg)
        ids = {m["runtime_id"] for m in engine_select._apply_filters(members, cfg)}
        assert good in ids
        assert degraded not in ids, "degraded 被派发选中会空转到 stale 判死"
        assert unavailable not in ids
    finally:
        _cleanup(cid)


def test_pool_reachability_uses_backend_gate():
    cid = _insert_credential()
    open_until = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    degraded = _insert_runtime_full(cid, "codearts-cred0", backend_status="degraded",
                                    until=open_until, fail_count=1)
    good = _insert_runtime_full(cid, "codearts-cred1")
    try:
        with db_lock:
            assert service._check_pool_engines_reachable([degraded], "all") is False
            assert service._check_pool_engines_reachable([degraded, good], "all") is True
    finally:
        _cleanup(cid)


def test_degraded_after_cooldown_reaches_half_open_claim():
    """R1 端到端：degraded 短冷却结束后必须能真正认领一次（否则半开通道形同虚设）。"""
    cid = _insert_credential()
    rid = _insert_runtime_full(cid, "codearts-cred0", backend_status="degraded",
                               until=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                               fail_count=1)
    try:
        assert service.claim_by_tag("all", rid) is None, "冷却期内不得认领"
        _expire_circuit(rid)
        task_id = _insert_pool_task()
        task = service.claim_by_tag("all", rid)
        assert task and task["id"] == task_id, "冷却结束后应放行一次半开试探"
    finally:
        _cleanup(cid)
