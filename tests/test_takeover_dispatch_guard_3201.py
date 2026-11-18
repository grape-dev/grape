"""Issue #3201：接管 active 期间统一拦截自动派发。

覆盖：
- webhook / pipeline 入队（_enqueue_pipeline_task）不建 task，事件置 skipped_takeover；
- direct 派发（_create_direct_task）同样被守门；
- token 过期按不活跃处理（自愈），不永久冻结 issue；
- 同仓其他 issue 不受影响；
- A2A 入口返回 409 already_taken_over；
- 状态聚合跳过 skipped_takeover，active 接管节点锁定 running；
- finish 先关会话再派发，并回传本次已忽略条数。
"""
import time

import pytest

from src.kernel.session_manager import db, db_lock

REPO = "gitcode:takeover3201/repo"
ISSUE = "#3201"
OTHER_ISSUE = "#3202"


def _clean():
    with db_lock:
        c = db.cursor()
        for table in ("takeover_sessions", "agent_tasks", "events", "issue"):
            try:
                c.execute(f"DELETE FROM {table} WHERE repo_key=?", (REPO,))
            except Exception:
                pass
        db.commit()


def _seed_event(seq, status="pending", agent="coding", issue=ISSUE):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, REPO, "a2a_event", issue, agent, status, "a2a", "t", "{}"),
        )
        db.commit()


def _seed_takeover(seq, agent="coding", issue=ISSUE, expires_in=3600, status="active",
                   session_id="tkos_3201"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM takeover_sessions WHERE id=?", (session_id,))
        now = int(time.time())
        c.execute(
            "INSERT INTO takeover_sessions "
            "(id, active_key, task_id, event_seq, repo_key, issue_id, agent_type, user_id, username, "
            " status, token_sha256, token_expires_at, token_issued_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, session_id if status == "active" else None, "t_3201", seq, REPO, issue,
             agent, 1, "tester", status, "x", now + expires_in, now),
        )
        db.commit()
    return session_id


def _event_status(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM events WHERE seq=?", (seq,))
        row = c.fetchone()
        return row[0] if row else None


def _task_count(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM agent_tasks WHERE event_seq=?", (seq,))
        return int(c.fetchone()[0])


@pytest.fixture(autouse=True)
def _cleanup():
    _clean()
    yield
    _clean()


def test_webhook_enqueue_skips_task_under_active_takeover(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep

    seq = 770001
    _seed_event(seq, status="pending")
    _seed_takeover(seq)

    def _boom(**kw):
        raise AssertionError("active takeover 期间不得创建任务")

    monkeypatch.setattr("src.domains.runtime.engine.service.enqueue_task", _boom)

    ep._enqueue_pipeline_task(
        REPO, seq, "coding", "opencode", "implementation",
        {"type": "issue_comment", "repository": "takeover3201/repo", "issue_id": ISSUE, "payload": {}},
        0, "", 1, ISSUE,
    )

    assert _event_status(seq) == "skipped_takeover"
    assert _task_count(seq) == 0


def test_direct_task_skips_under_active_takeover(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep

    seq = 770002
    _seed_event(seq, status="running")
    _seed_takeover(seq)

    def _boom(**kw):
        raise AssertionError("active takeover 期间不得创建任务")

    monkeypatch.setattr("src.domains.runtime.engine.service.enqueue_task", _boom)

    task_id = ep._create_direct_task(
        REPO, seq, "coding", "opencode", "implementation", "implementation", 1,
        {"type": "a2a_event", "issue_id": ISSUE, "payload": {}}, ISSUE,
    )

    assert task_id == ""
    assert _event_status(seq) == "skipped_takeover"
    assert _task_count(seq) == 0


def test_expired_takeover_is_inactive_and_self_heals():
    from src.domains.runtime.engine import takeover as svc

    seq = 770003
    _seed_event(seq, status="pending")
    sid = _seed_takeover(seq, expires_in=-10)

    assert svc.active_takeover_for(REPO, ISSUE) is None
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, active_key FROM takeover_sessions WHERE id=?", (sid,))
        assert c.fetchone() == ("expired", None)


def test_takeover_on_one_issue_does_not_block_another():
    from src.domains.runtime.engine import takeover as svc

    _seed_event(770004, status="pending", issue=ISSUE)
    _seed_takeover(770004, issue=ISSUE)

    assert svc.active_takeover_for(REPO, OTHER_ISSUE) is None
    assert svc.active_takeover_for(REPO, ISSUE) is not None


def test_a2a_endpoint_returns_409_already_taken_over():
    from fastapi.testclient import TestClient
    from src.api.main import app

    seq = 770005
    _seed_event(seq, status="takeover")
    _seed_takeover(seq)

    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                "VALUES (?, 'takeover3201', 'repo', 'takeover3201/repo')", (REPO,))
        except Exception:
            pass
        db.commit()

    client = TestClient(app)
    r = client.post("/api/a2a", json={
        "agent": "coding",
        "payload": {
            "repository": "takeover3201/repo",
            "issue_number": "3201",
            "context": "接管期间重复派发",
        },
    })
    assert r.status_code == 409, r.text
    assert "already_taken_over" in r.text


def test_pipeline_status_ignores_skipped_and_locks_running():
    from src.api.pipeline_status import (
        STATUS_COMPLETED, STATUS_RUNNING, compute_node_statuses,
    )

    events = [
        {"seq": 1, "agent_type": "coding", "status": "completed"},
        {"seq": 2, "agent_type": "coding", "status": "skipped_takeover"},
    ]
    nodes, _ = compute_node_statuses([{"id": "coding"}], events, current_stage="coding")
    assert nodes[0]["node_status"] == STATUS_COMPLETED

    _seed_event(770006, status="takeover")
    _seed_takeover(770006)
    events = [
        {"seq": 770006, "agent_type": "coding", "status": "completed"},
        {"seq": 770007, "agent_type": "coding", "status": "skipped_takeover"},
    ]
    nodes, _ = compute_node_statuses(
        [{"id": "coding"}], events, current_stage="coding",
        repo_key=REPO, issue_id=ISSUE,
    )
    assert nodes[0]["node_status"] == STATUS_RUNNING
    assert nodes[0].get("takeover") is True


def test_finish_returns_skipped_count_and_closes_before_dispatch(monkeypatch):
    from src.domains.runtime.engine import takeover as svc

    base_seq = 770010
    _seed_event(base_seq, status="takeover")
    sid = _seed_takeover(base_seq)
    _seed_event(base_seq + 1, status="skipped_takeover")
    _seed_event(base_seq + 2, status="skipped_takeover")

    monkeypatch.setattr(svc, "_advance_stage", lambda *a, **k: ("qa", False))

    seen = {}

    def _fake_dispatch(**kw):
        seen["active"] = svc.active_takeover_for(REPO, ISSUE)
        return {"ok": True, "seq": base_seq + 99}

    monkeypatch.setattr(svc, "_dispatch_next", _fake_dispatch)

    res = svc.finish_takeover(
        session_id=sid, user={"id": 1, "username": "tester"},
        outcome="completed", summary="done",
    )

    assert seen["active"] is None, "finish 必须先关闭会话再派发，守门不得拦住自己"
    assert res["skipped_dispatches"] == 2
    assert "2" in res["skipped_message"]
    assert res["session"]["skipped_dispatches"] == 2
