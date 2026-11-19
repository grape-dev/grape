"""Issue #3201：接管 active 期间统一拦截自动派发。

覆盖：
- webhook / pipeline 入队（_enqueue_pipeline_task）不建 task，事件置 skipped_takeover；
- direct 派发（_create_direct_task）同样被守门；
- 手动重派（redispatch_event，含 local_only）同样被守门，接管结束后正常；
- autopilot 派发同样被守门（execution 置 skipped），接管结束后正常；
- A2A direct/remote 派发返回 skipped 且不启动 agent，队列 drain 不把 skipped 判成失败；
- token 过期按不活跃处理（自愈），不永久冻结 issue；
- 同仓其他 issue 不受影响；
- A2A 入口返回 409 already_taken_over；
- 状态聚合跳过 skipped_takeover，active 接管节点锁定 running；
- finish 先关会话再派发，并回传本次已忽略条数。
"""
import time
from unittest.mock import MagicMock

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
        try:
            c.execute("DELETE FROM autopilot_executions WHERE id BETWEEN 770000 AND 779999")
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


def _seed_task(seq, task_id, status="running", runtime_id=0, agent="coding", issue=ISSUE):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, repo_key, "
            "issue_id, agent_type, prompt, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, f"s_{task_id}", runtime_id, seq, REPO, issue, agent, "p", status),
        )
        db.commit()


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


def test_expired_takeover_in_repo_self_heals_and_does_not_block_autopilot():
    """R2 自愈：仓储级守门遇过期 active 会话必须真转 expired，且不再拦截自动派发。

    回归点（PR #1676 第 6 轮复审）：active_takeover_in_repo 的自愈 UPDATE 占位符 4 个、
    绑定参数只有 3 个（缺 repo_key），sqlite3 每次抛 ProgrammingError 被 except 吞掉，
    自愈永不生效（过期会话仍为 active，仓储级守门持续冻结 autopilot）。
    """
    from src.domains.runtime.engine import takeover as svc
    from src.domains.runtime.engine import scheduler as sch

    exec_id = 770410
    sid = _seed_takeover(exec_id, agent="coding", issue=ISSUE, expires_in=-10,
                         session_id="tkos_3201_exp_repo")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (exec_id,))
        c.execute("INSERT INTO autopilot_executions (id, task_id, status) VALUES (?,?,?)",
                  (exec_id, 5, "pending"))
        db.commit()

    assert svc.active_takeover_in_repo(REPO) is None
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, active_key FROM takeover_sessions WHERE id=?", (sid,))
        assert c.fetchone() == ("expired", None), "过期仓储级会话必须自愈为 expired"

    assert sch._skip_autopilot_for_takeover(REPO, {"id": 5}, exec_id) is False
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM autopilot_executions WHERE id=?", (exec_id,))
        assert c.fetchone()[0] == "pending", "过期接管不得拦截 autopilot 派发"


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


def test_run_pipeline_engine_unavailable_under_takeover_marks_skipped(monkeypatch):
    """审查修复：守门必须在引擎可用性判定之前。

    引擎不可用的早退分支原会 `_create_failed_task` + `_fail_event`，把接管期间
    本应跳过的事件落成 failed 并留下 failed task 快照；守门前移后应统一落
    skipped_takeover 且不建任何 task。
    """
    import json

    from src.domains.runtime.pipeline import event_pipeline as ep

    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO event_route_config (repo_key, event_type, skill) "
                  "VALUES ('','issue_opened','issue')")
        c.execute(
            "INSERT OR REPLACE INTO agent_type (agent_type, display_name, enable, order_num, subscribe_events) "
            "VALUES ('issue','Issue Agent',1,2,'[\"issue_opened\",\"issue_updated\",\"issue_comment\"]')"
        )
        db.commit()

    _seed_takeover(770020, agent="issue", session_id="tkos_3201_run")

    # 模拟引擎全部不可用：若守门仍只在建 task 出口，会走 failed 分支。
    monkeypatch.setattr(
        "src.domains.runtime.engine.engine_select.pick_engine",
        lambda *a, **k: None,
    )

    raw = json.dumps({
        "object_kind": "issue",
        "project": {"path_with_namespace": "takeover3201/repo"},
        "object_attributes": {"id": 3201, "iid": 3201, "title": "T", "action": "open"},
        "user": {"username": "alice"},
    }).encode()
    res = ep.run_pipeline(
        raw, {"X-GitCode-Event": "issue_opened"},
        account="alice", repo_key_override="takeover3201/repo",
    )
    assert res.get("skipped") == "already_taken_over", res

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT seq, status FROM events WHERE repo_key=? AND issue_id=? "
            "ORDER BY seq DESC LIMIT 1", (REPO, ISSUE),
        )
        seq, status = c.fetchone()
        c.execute("SELECT COUNT(*) FROM agent_tasks WHERE repo_key=? AND event_seq=?", (REPO, seq))
        tasks = int(c.fetchone()[0])
    assert status == "skipped_takeover", status
    assert tasks == 0, "接管期间引擎不可用也不得建 failed task"


def test_skipped_dispatch_count_stops_at_next_takeover():
    """审查修复：已结束会话的忽略计数必须冻结在下一次接管边界，不被累加。"""
    from src.domains.runtime.engine import takeover as svc

    first_seq = 770100
    _seed_takeover(first_seq, session_id="tkos_3201_a")
    _seed_event(first_seq + 1, status="skipped_takeover")
    _seed_event(first_seq + 2, status="skipped_takeover")
    first_sess = svc.get_session("tkos_3201_a")
    assert svc.skipped_dispatch_count(first_sess) == 2

    # 同一 Issue 的后续接管产生新的 skipped 事件，不得累加到上一会话。
    _seed_takeover(first_seq + 10, session_id="tkos_3201_b")
    _seed_event(first_seq + 11, status="skipped_takeover")
    _seed_event(first_seq + 12, status="skipped_takeover")

    assert svc.skipped_dispatch_count(first_sess) == 2, "已结束会话计数必须冻结在下一次接管前"
    second_sess = svc.get_session("tkos_3201_b")
    assert svc.skipped_dispatch_count(second_sess) == 2


# ===== R1：手动重派（redispatch_event）统一守门 =====


def test_redispatch_blocked_under_active_takeover(monkeypatch):
    """R1：接管 active 期间重派（含 local_only）→ 409，agent_tasks 零新增。"""
    from fastapi import HTTPException

    from src.api import deps as _deps
    from src.domains.runtime.engine import service as svc
    from src.domains.runtime.routes import events as ev

    seq = 770200
    _seed_event(seq, status="failed")
    _seed_takeover(seq)
    monkeypatch.setattr(_deps, "check_repo_access", lambda *a, **k: None)
    monkeypatch.setattr(ev, "_audit_admin_action", lambda *a, **k: None)

    def _boom(*a, **k):
        raise AssertionError("接管 active 期间重派不得建 task / 起 agent")

    monkeypatch.setattr(svc, "enqueue_task", _boom)
    monkeypatch.setattr(svc, "requeue_task", _boom)

    for local_only in (False, True):
        with pytest.raises(HTTPException) as ei:
            ev.redispatch_event(seq, local_only=local_only, user={"username": "tester"})
        assert ei.value.status_code == 409, ei.value.detail
        assert "already_taken_over" in str(ei.value.detail)
        assert _task_count(seq) == 0
    # 事件状态不改写（重派被拦截不落任何派发态）
    assert _event_status(seq) == "failed"


def test_redispatch_allowed_after_takeover_closed(monkeypatch):
    """R1 对照组：接管已结束 → 同一重派正常推进（证明不是一刀切拦截）。"""
    from src.api import deps as _deps
    from src.api import event_store as est
    from src.domains.mgmt.user import db as udb
    from src.domains.runtime.engine import service as svc
    from src.domains.runtime.pipeline import event_pipeline as ep
    from src.domains.runtime.routes import events as ev

    seq = 770201
    _seed_event(seq, status="running")
    _seed_task(seq, "t_770201", status="running", runtime_id=7)
    _seed_takeover(seq, status="finished", session_id="tkos_3201_closed")
    monkeypatch.setattr(_deps, "check_repo_access", lambda *a, **k: None)
    monkeypatch.setattr(ev, "_audit_admin_action", lambda *a, **k: None)
    monkeypatch.setattr(udb, "get_meta_value", lambda k: "")
    monkeypatch.setattr(udb, "set_meta_value", lambda k, v: None)
    monkeypatch.setattr(ep, "is_plugin_mode", lambda: False)
    monkeypatch.setattr(est, "reset_event_terminal", MagicMock())
    _requeue = MagicMock()
    monkeypatch.setattr(svc, "requeue_task", _requeue)

    res = ev.redispatch_event(seq, user={"username": "tester"})

    assert res["status"] == "redispatched"
    assert res["task_id"] == "t_770201"
    _requeue.assert_called_once_with("t_770201")


# ===== R2：autopilot 派发统一守门 =====


def test_autopilot_dispatch_skips_under_active_takeover():
    """R2：接管 active 期间 autopilot 不建 task，execution 置 skipped。

    用真实 Issue 接管会话（#3201）触发：autopilot 无 issue 维度，守门按仓库判定，
    不能再用合成键 #autopilot-x 自证。
    """
    from src.domains.runtime.engine import scheduler as sch

    exec_id = 770400
    task = {"id": 3}
    _seed_takeover(exec_id, agent="coding", issue=ISSUE,
                   session_id="tkos_3201_ap")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (exec_id,))
        c.execute("INSERT INTO autopilot_executions (id, task_id, status) VALUES (?,?,?)",
                  (exec_id, task["id"], "pending"))
        db.commit()

    sch._execute_autopilot_remote(
        REPO, task, exec_id, "issue", "", 0, "opencode", "inst", "/tmp/ws", "p",
        "", "Agent", {}, "takeover3201/repo", {},
    )

    assert _task_count(exec_id) == 0, "接管期间 autopilot 不得建 task"
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, error FROM autopilot_executions WHERE id=?", (exec_id,))
        status, error = c.fetchone()
    assert status == "skipped", f"execution 应为 skipped，实际 {status}"
    assert "接管进行中" in (error or "")


def test_autopilot_dispatch_allowed_after_takeover_closed(monkeypatch):
    """R2 对照组：接管已结束 → autopilot 正常入队（证明不是一刀切）。"""
    from src.api import session_store as sst
    from src.domains.runtime.engine import scheduler as sch
    from src.domains.runtime.engine import service as svc
    from src.kernel import prompt_builder as pb

    exec_id = 770401
    task = {"id": 4, "created_by": 0}
    _seed_takeover(exec_id, agent="coding", issue=ISSUE,
                   status="finished", session_id="tkos_3201_ap_closed")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (exec_id,))
        c.execute("INSERT INTO autopilot_executions (id, task_id, status) VALUES (?,?,?)",
                  (exec_id, task["id"], "pending"))
        db.commit()

    monkeypatch.setattr(sst, "get_or_create_agent_session", lambda *a, **k: {"id": "as_ap"})
    monkeypatch.setattr(sst, "get_or_create_issue_session", lambda *a, **k: {"id": "is_ap"})
    monkeypatch.setattr(pb, "_resolve_server_url", lambda *a, **k: "")
    _enq = MagicMock(return_value="t_ap_ok")
    monkeypatch.setattr(svc, "enqueue_task", _enq)

    sch._execute_autopilot_remote(
        REPO, task, exec_id, "issue", "", 0, "opencode", "inst", "/tmp/ws", "p",
        "", "Agent", {}, "takeover3201/repo", {},
    )

    _enq.assert_called_once()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM autopilot_executions WHERE id=?", (exec_id,))
        assert c.fetchone()[0] == "dispatched"


def test_execute_task_stops_on_skipped_without_retry(monkeypatch):
    """R2：skipped 是守门终止态，不得进入重试循环（不新建 execution）。"""
    import asyncio

    from src.domains.runtime.engine import scheduler as sch

    created: list[int] = []

    def _create_execution(task_id, agent_type):
        exec_id = 770500 + len(created)
        created.append(exec_id)
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM autopilot_executions WHERE id=?", (exec_id,))
            c.execute("INSERT INTO autopilot_executions (id, task_id, status) VALUES (?,?,?)",
                      (exec_id, int(task_id), "running"))
            db.commit()
        return exec_id

    def _run_agent(repo_key, task, exec_id):
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE autopilot_executions SET status='skipped', "
                      "error='接管进行中，自动派发已拦截' WHERE id=?", (exec_id,))
            db.commit()

    monkeypatch.setattr(sch, "_create_execution", _create_execution)
    monkeypatch.setattr(sch, "_execute_autopilot_agent", _run_agent)
    monkeypatch.setattr(sch, "_compute_next_run", lambda cron: "")
    monkeypatch.setattr(sch, "_update_task_run_info", lambda *a, **k: None)
    monkeypatch.setattr(sch, "_BASE_RETRY_DELAY", 0)
    monkeypatch.setattr(sch, "_MAX_RETRY_DELAY", 0)

    task = {"id": 771, "name": "ap", "repo_key": REPO, "max_retry": 2, "cron": "* * * * *"}
    asyncio.run(sch._execute_task(task))

    assert len(created) == 1, f"skipped 不得重试，实际创建 {len(created)} 条 execution"
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM autopilot_executions WHERE id=?", (created[0],))
        assert c.fetchone()[0] == "skipped"


# ===== R3：A2A direct / remote / 队列 drain 统一守门 =====


def _patch_a2a_legacy(monkeypatch, kind):
    from src.domains.runtime.engine import engine_select as es
    from src.domains.runtime.engine import service as svc
    from src.domains.runtime.pipeline import event_pipeline as ep

    monkeypatch.setattr(ep, "is_plugin_mode", lambda: False)
    monkeypatch.setattr(es, "pick_engine", lambda *a, **k: 42)
    monkeypatch.setattr(es, "engine_name", lambda *a, **k: "opencode")
    monkeypatch.setattr(es, "get_scene_pool_engine_ids", lambda *a, **k: [])
    monkeypatch.setattr(svc, "runtime_execution_kind", lambda rid: kind)
    return ep


def test_a2a_direct_skips_and_does_not_start_agent_under_takeover(monkeypatch):
    """R3：direct 分支守门命中后必须不调用 start_direct_agent。"""
    from src.api import a2a_dispatch as ad

    seq = 770300
    _seed_event(seq, status="running")
    _seed_takeover(seq)
    ep = _patch_a2a_legacy(monkeypatch, "direct")
    _started = MagicMock()
    monkeypatch.setattr(ep, "start_direct_agent", _started)

    res = ad.run_a2a_dispatch(
        REPO, seq, "coding", {"type": "a2a_event", "issue_id": ISSUE, "payload": {}},
        skill_name="", issue_number="3201", repository="takeover3201/repo",
    )

    assert res["status"] == "skipped", res
    assert res.get("seq") == seq
    _started.assert_not_called()
    assert _task_count(seq) == 0
    assert _event_status(seq) == "skipped_takeover"


def test_a2a_remote_returns_skipped_under_takeover(monkeypatch):
    """R3：remote/广播分支入队被守门 → 返回非 ok（skipped），不建 task。"""
    from src.api import a2a_dispatch as ad

    seq = 770301
    _seed_event(seq, status="running")
    _seed_takeover(seq)
    _patch_a2a_legacy(monkeypatch, "queue_remote")

    res = ad.run_a2a_dispatch(
        REPO, seq, "coding", {"type": "a2a_event", "issue_id": ISSUE, "payload": {}},
        skill_name="", issue_number="3201", repository="takeover3201/repo",
    )

    assert res["status"] == "skipped", res
    assert res.get("seq") == seq
    assert _task_count(seq) == 0
    assert _event_status(seq) == "skipped_takeover"


def test_a2a_queue_drain_skips_without_failing_event(monkeypatch):
    """R3：队列已有 A2A 事件 → drain 后零 task、事件不被判 failed、不起 agent。"""
    from src.domains.runtime.pipeline import event_pipeline as ep

    qseq = 770302
    _seed_event(qseq, status="queued")
    _seed_takeover(qseq)
    _patch_a2a_legacy(monkeypatch, "direct")
    _started = MagicMock()
    monkeypatch.setattr(ep, "start_direct_agent", _started)

    ok = ep._claim_and_dispatch_queued(
        qseq, REPO, "coding", ISSUE,
        {"issue_number": "3201"},
        {"dispatch": {"skill_name": "", "runtime_id": 42, "backend": "opencode",
                      "evt": {"type": "a2a_event", "issue_id": ISSUE}}},
        0,
    )

    assert ok is True, "skipped 应被 drain 视为已处理，不得走 _fail_event"
    _started.assert_not_called()
    assert _task_count(qseq) == 0
    assert _event_status(qseq) == "skipped_takeover", "drain 不得把 skipped 改成 failed"
