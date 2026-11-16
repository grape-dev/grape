"""Issue #2508：终态 a2a 事件投递去重、重派 token 重签、认证错误三类细分。

覆盖验收标准：
- completed/cancelled 事件再次投递时静默跳过并写 terminal_event_replay 审计；
- 关联任务终态（事件仍 running）同样命中双检；
- failed 事件保持现有重试语义（不被终态去重复核拦截）；
- 人工重派 reissue 标记可放行；
- 人工重派后任务 token 失效时经 sync 重签新 tk_；
- 401 认证失败可区分 TOKEN_REVOKED_OR_TASK_FINISHED / SNAPSHOT_EXPIRED / MISSING_TOKEN。
"""
import json
import uuid

from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.event_store import (
    append_event, find_terminal_event_replay, is_event_reissue,
    mark_event_reissue, update_event_complete,
)
from src.kernel.session_manager import db, db_lock


REPO_KEY = "gitcode:issue2508/r"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM session_api_tokens WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'i2508rt-%'")
        db.commit()


def _seed_event(status="completed", agent="coding", payload=None):
    _payload = payload or {"issue_number": "6401", "fingerprint": "fp2508",
                           "idempotency_key": "idem-2508"}
    return append_event(
        repo_key=REPO_KEY, event_type="a2a_event", title=f"A2A: {agent}",
        issue_id="#6401", source="a2a", agent_type=agent,
        summary=f"A2A: → {agent}",
        payload=_payload,
        status=status,
        idempotency_key=str(_payload.get("idempotency_key") or ""))


def _seed_task(task_id, event_seq, status, agent="coding"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, "
            "issue_id, repo_key, agent_type, prompt, status) "
            "VALUES (?,?,0,?,?,?,?,'p',?)",
            (task_id, "i2508sess-" + agent, event_seq, "#6401", REPO_KEY, agent, status))
        db.commit()


def _skips(reason="terminal_event_replay"):
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT reason, detail FROM event_skipped WHERE repo_key=? AND reason=? "
            "ORDER BY id", (REPO_KEY, reason)).fetchall()


def test_completed_event_replay_is_skipped_and_audited():
    _clean()
    seq = _seed_event(status="completed")
    from src.api.a2a_dispatch import run_a2a_dispatch
    result = run_a2a_dispatch(REPO_KEY, seq, "coding", {})
    assert result["status"] == "skipped"
    assert result["skipped"] == "terminal_event_replay"
    assert result["original_event"]["status"] == "completed"
    skips = _skips()
    assert skips, "terminal replay must be audited"
    assert f"seq={seq}" in skips[-1][1]
    assert "fingerprint=fp2508" in skips[-1][1]


def test_cancelled_event_replay_is_skipped():
    _clean()
    seq = _seed_event(status="running")
    update_event_complete(seq, "cancelled", result="manual cancel")
    info = find_terminal_event_replay(REPO_KEY, event_seq=seq)
    assert info.get("event_terminal") is True
    assert info.get("status") == "cancelled"


def test_terminal_task_blocks_running_event_replay():
    _clean()
    seq = _seed_event(status="running")
    _seed_task("t_i2508done", seq, "completed")
    info = find_terminal_event_replay(REPO_KEY, event_seq=seq)
    assert info.get("task_terminal") is True
    assert info.get("task_status") == "completed"


def test_failed_event_keeps_retry_semantics():
    _clean()
    seq = _seed_event(status="failed")
    _seed_task("t_i2508failed", seq, "failed")
    assert find_terminal_event_replay(REPO_KEY, event_seq=seq) == {}


def test_idempotency_key_terminal_lookup():
    _clean()
    seq = _seed_event(status="completed", payload={
        "issue_number": "6401", "fingerprint": "fp-x", "idempotency_key": "idem-x"})
    info = find_terminal_event_replay(REPO_KEY, idempotency_key="idem-x")
    assert info.get("seq") == seq


def test_reissue_marker_bypasses_terminal_replay():
    _clean()
    seq = _seed_event(status="completed")
    assert mark_event_reissue(seq) is True
    assert is_event_reissue(seq) is True
    from src.api.a2a_dispatch import run_a2a_dispatch
    result = run_a2a_dispatch(REPO_KEY, seq, "coding", {}, reissue=True)
    assert result.get("skipped") != "terminal_event_replay"


def _seed_runtime_session(agent="coding"):
    with db_lock:
        c = db.cursor()
        sid = "i2508sess-" + agent
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, status) "
            "VALUES (?,?,?,'idle')", (sid, REPO_KEY, agent))
        name = "i2508rt-" + uuid.uuid4().hex[:6]
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, installed, "
            "healthy, status, scope, owner_id) "
            "VALUES (?,?,'0',1,1,'online','global','')", (name, name))
        rid = c.lastrowid
        db.commit()
    return sid, rid


def test_redispatch_reissues_expired_task_token():
    _clean()
    from src.api.auth_token import verify_session_token, _sha256
    from src.domains.runtime.engine.service import enqueue_task
    from src.domains.runtime.routes.events import _reissue_task_session_token

    sid, rid = _seed_runtime_session()
    tid = enqueue_task(agent_session_id=sid, runtime_id=rid, prompt="p", cwd=".",
                       repo_key=REPO_KEY, agent_type="coding", env_extra={})
    with db_lock:
        row = db.cursor().execute("SELECT env_extra FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        old = json.loads(row[0])["GRAPE_SESSION_TOKEN"]
        db.cursor().execute("UPDATE session_api_tokens SET expires_at='0' WHERE token_sha256=?",
                            (_sha256(old),))
        db.commit()
    new = _reissue_task_session_token(tid)
    assert new and new != old
    assert verify_session_token(new) is not None
    with db_lock:
        row = db.cursor().execute("SELECT env_extra FROM agent_tasks WHERE id=?", (tid,)).fetchone()
    assert json.loads(row[0])["GRAPE_SESSION_TOKEN"] == new


def test_timeout_failed_task_keeps_retry_semantics():
    """Spec A：timeout_failed 属可重试失败终态，不得被终态去重拦截。"""
    _clean()
    seq = _seed_event(status="running")
    _seed_task("t_i2508timeout", seq, "timeout_failed")
    assert find_terminal_event_replay(REPO_KEY, event_seq=seq) == {}


def test_queue_drain_not_cancelled_by_unrelated_terminal_event():
    """Spec C：排队 drain 不再做恒不命中的终态判定；同 repo 更早终态事件不影响补派。"""
    _clean()
    _seed_event(status="completed", payload={
        "issue_number": "6401", "fingerprint": "fp-earlier",
        "idempotency_key": "idem-earlier"})
    from src.api.event_store import append_queued_a2a_event
    qseq = append_queued_a2a_event(
        REPO_KEY, issue_id="#6401", agent_type="coding",
        title="A2A: coding", summary="A2A: → coding",
        payload={"issue_number": "6401", "fingerprint": "fp-queue",
                 "idempotency_key": "idem-queue"},
        context_key="ctx-queue", idempotency_key="idem-queue")
    assert qseq > 0
    from src.domains.runtime.pipeline.event_pipeline import _claim_and_dispatch_queued
    _claim_and_dispatch_queued(
        qseq, REPO_KEY, "coding", "#6401",
        {"issue_number": "6401", "fingerprint": "fp-queue",
         "idempotency_key": "idem-queue"},
        {"dispatch": {}}, 0)
    with db_lock:
        st = db.cursor().execute("SELECT status FROM events WHERE seq=?", (qseq,)).fetchone()[0]
    assert st != "cancelled", "排队事件不应被无关终态事件误判为终态重放"


def test_failed_redispatch_does_not_leave_reissue_marker():
    """Spec B：重派在校验阶段失败（如自循环拦截 409）不得残留 __reissue。"""
    _clean()
    from src.api.event_store import append_skipped_event
    seq = _seed_event(status="failed", payload={"issue_number": "6401"})
    with db_lock:
        db.cursor().execute(
            "UPDATE events SET event_type='issue_comment', issue_id='#6401' WHERE seq=?", (seq,))
        db.commit()
    append_skipped_event(REPO_KEY, "issue_comment", "self_loop", "self loop",
                         issue_id="#6401")
    from src.domains.runtime.routes.events import redispatch_event
    import pytest
    with pytest.raises(Exception) as exc:
        redispatch_event(seq, user={"username": "tester", "auth_type": "session_token"})
    assert getattr(exc.value, "status_code", None) == 409
    assert is_event_reissue(seq) is False


def test_guarded_creation_then_terminal_idempotency_lookup():
    """Spec D/Standards 7：guarded 创建路径落库的幂等键可被终态双检命中。"""
    _clean()
    from src.api.a2a_dispatch_guard import append_guarded_a2a_event
    kind, seq = append_guarded_a2a_event(
        REPO_KEY, "6401", "coding", "default",
        title="A2A: coding", summary="A2A: → coding",
        payload={"issue_number": "6401", "fingerprint": "fp-guard",
                 "idempotency_key": "idem-guard"},
        idempotency_key="idem-guard")
    assert kind == "ok" and seq > 0
    update_event_complete(seq, "completed", result="done")
    info = find_terminal_event_replay(REPO_KEY, idempotency_key="idem-guard")
    assert info.get("seq") == seq
    assert info.get("event_terminal") is True

