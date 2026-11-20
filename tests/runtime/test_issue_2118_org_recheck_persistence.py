# -*- coding: utf-8 -*-
"""Issue #2118 复审：自动推进（plugin 异步）stage 语义与聚合重排持久化。

覆盖：
- plugin/queue_remote 事件驱动路径：run_a2a_dispatch 只「已发 handoff/已入队」时
  stage 不提前写；下游确认失败则保持当前节点并留痕（真实 append_event + _fail_event）。
- 同步确认路径：先写 stage，后续失败则回滚到原节点。
- 组织聚合下一轮不再依赖进程内 threading.Timer：落成持久化 queued 事件（重启可恢复）。
- 巡检 reconcile 到点补派延迟重排事件；派发失败按退避重排、耗尽后转 pending_review。
- listener 真实异步失败（failed/blocked/异常）走同一重排闭环；仅 dispatched 才推进 stage。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import src.api.a2a_dispatch as a2a_dispatch
import src.api.pipeline_config as pipeline_config
import src.api.session_store as session_store
from src.api import db_setup
from src.domains.runtime.engine.service import (
    _auto_advance_after_complete,
    _schedule_aggregate_recheck,
)
from src.domains.runtime.pipeline.event_pipeline import (
    _fail_event,
    reconcile_queued_a2a,
)
from src.api.patrol import l1_event_exists
from src.api.event_store import append_event
from src.kernel.session_manager import db, db_lock


REPO = "gitcode:issue2118recheck/repo"
ISSUE_ID = "#42"
SCENE_ID = "custom-org-issue2118-recheck"

ORG_NODES = [
    {
        "id": "org_intake", "tag": "issue", "skill": "org-intake-cli",
        "mode": "emit", "next": "org_dispatch", "handoff": "a2a",
        "skip": [], "enabled": True, "engine_runtime_ids": [],
    },
    {
        "id": "org_dispatch", "tag": "coding", "skill": "org-dispatch-cli",
        "mode": "emit", "next": "org_aggregate", "handoff": "a2a",
        "skip": [], "enabled": True, "engine_runtime_ids": [],
    },
    {
        "id": "org_aggregate", "tag": "qa", "skill": "org-aggregate-cli",
        "mode": "emit", "next": "done", "handoff": "complete",
        "skip": [], "enabled": True, "engine_runtime_ids": [],
    },
]

TRANSITION_INTAKE = {
    "node": ORG_NODES[0], "next_node": ORG_NODES[1], "next_id": "org_dispatch",
    "next_role": "coding", "terminal": False, "handoff": "a2a",
}
TRANSITION_AGGREGATE = {
    "node": ORG_NODES[2], "next_node": None, "next_id": "done",
    "next_role": "done", "terminal": True, "handoff": "complete",
}


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        db.commit()
    pipeline_config.invalidate_scene_cache()


def _task(agent="issue"):
    return {"id": "t_2118r", "repo_key": REPO, "issue_id": ISSUE_ID,
            "agent_type": agent, "event_seq": 900, "account": "issue2118-owner"}


def _patch_transition(monkeypatch, transition):
    monkeypatch.setattr(pipeline_config, "get_pipeline_transition",
                        lambda *a, **kw: transition)
    import src.api.issues_center as ic
    monkeypatch.setattr(ic, "_get_issue_title", lambda *a, **kw: "组织需求")


def _event_row(seq):
    with db_lock:
        row = db.cursor().execute(
            "SELECT status, payload FROM events WHERE seq=?", (int(seq),)).fetchone()
    if not row:
        return None, None
    try:
        return row[0], json.loads(row[1] or "{}")
    except Exception:
        return row[0], {}


def test_plugin_async_success_defers_stage_and_failure_keeps_stage(monkeypatch):
    """plugin 异步路径：不提前写 stage；listener 失败后 stage 仍为当前节点且留痕。"""
    session_store.set_issue_stage(REPO, ISSUE_ID, "issue", "issue")
    _patch_transition(monkeypatch, TRANSITION_INTAKE)
    captured = {}

    def _fake_dispatch(repo_key, seq, *a, **kw):
        captured["seq"] = seq
        return {"status": "ok", "seq": seq, "async": True, "mode": "event-driven"}

    monkeypatch.setattr(a2a_dispatch, "run_a2a_dispatch", _fake_dispatch)

    out = _auto_advance_after_complete(_task("issue"))

    assert out["action"] == "a2a"
    seq = captured["seq"]
    assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "issue"
    status, payload = _event_row(seq)
    assert status == "running"
    assert payload["stage_guard"] == {"from": "issue", "to": "coding"}

    _fail_event(seq, "A2A 交办派发失败（target=coding）", diag={"code": "a2a_dispatch_failed"})

    status, payload = _event_row(seq)
    assert status == "failed", "失败事件必须留痕"
    assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "issue", \
        "下游 failed 后 stage 不得指向未运行的下游"


def test_sync_auto_advance_advances_then_failure_rolls_back(monkeypatch):
    """同步确认路径：先推进 stage，后续失败回滚到原节点。"""
    session_store.set_issue_stage(REPO, ISSUE_ID, "issue", "issue")
    _patch_transition(monkeypatch, TRANSITION_INTAKE)
    captured = {}

    def _fake_dispatch(repo_key, seq, *a, **kw):
        captured["seq"] = seq
        return {"status": "ok", "seq": seq}

    monkeypatch.setattr(a2a_dispatch, "run_a2a_dispatch", _fake_dispatch)

    out = _auto_advance_after_complete(_task("issue"))

    assert out["action"] == "a2a"
    assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "coding"

    _fail_event(captured["seq"], "下游任务失败", diag={"code": "a2a_dispatch_failed"})

    assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "issue", \
        "同步路径推进后失败必须回滚 stage"


def test_aggregate_recheck_persists_queued_event(monkeypatch):
    """聚合下一轮落成持久化 queued 事件（重启可恢复），而不是仅内存定时器。"""
    timers = []
    monkeypatch.setattr(
        "src.domains.runtime.engine.service._start_timer",
        lambda delay, fn, args: timers.append(args) or object())

    ok = _schedule_aggregate_recheck(_task("qa"), TRANSITION_AGGREGATE, SCENE_ID, 7)
    assert ok is True

    with db_lock:
        rows = db.cursor().execute(
            "SELECT seq, status, payload FROM events WHERE repo_key=? AND status='queued'",
            (REPO,)).fetchall()
    assert len(rows) == 1, "必须有一条持久化 queued 重排事件"
    seq, status, payload_raw = rows[0]
    payload = json.loads(payload_raw)
    assert status == "queued"
    assert payload["aggregate_recheck"] is True
    assert payload["aggregate_round"] == 7
    assert payload["_not_before"], "必须带延迟到点时间"
    assert payload["_a2a_queue"]["dispatch"]["skill_name"] == "org-aggregate-cli"
    assert timers and timers[0][3] == 7


def test_reconcile_dispatches_due_delayed_recheck(monkeypatch):
    """巡检 reconcile 到点后补派延迟重排事件（定时器丢失/重启后的恢复路径）。"""
    monkeypatch.setattr(
        "src.domains.runtime.engine.service._start_timer",
        lambda delay, fn, args: object())
    session_store.set_issue_stage(REPO, ISSUE_ID, "qa", "qa")
    assert _schedule_aggregate_recheck(_task("qa"), TRANSITION_AGGREGATE, SCENE_ID, 1)

    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT seq, payload FROM events WHERE repo_key=? AND status='queued'",
                        (REPO,)).fetchone()
        payload = json.loads(row[1])
        payload["_not_before"] = "2000-01-01T00:00:00+00:00"
        c.execute("UPDATE events SET payload=? WHERE seq=?",
                  (json.dumps(payload, ensure_ascii=False), row[0]))
        db.commit()
        seq = row[0]

    calls = []
    monkeypatch.setattr(a2a_dispatch, "run_a2a_dispatch",
                        lambda repo_key, qseq, *a, **kw: calls.append(qseq)
                        or {"status": "ok", "seq": qseq})

    dispatched = reconcile_queued_a2a(REPO)

    assert seq in dispatched
    status, _payload = _event_row(seq)
    assert status == "running"
    assert calls == [seq]


def test_recheck_dispatch_failure_degrades_to_pending_review(monkeypatch):
    """无引擎/派发失败：按退避重排，耗尽后转 pending_review，不静默停在 qa。"""
    monkeypatch.setenv("GRAPE_ORG_AGGREGATE_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(
        "src.domains.runtime.engine.service._start_timer",
        lambda delay, fn, args: object())
    session_store.set_issue_stage(REPO, ISSUE_ID, "qa", "qa")
    assert _schedule_aggregate_recheck(_task("qa"), TRANSITION_AGGREGATE, SCENE_ID, 1)

    monkeypatch.setattr(a2a_dispatch, "run_a2a_dispatch",
                        lambda repo_key, qseq, *a, **kw: {
                            "status": "failed", "seq": 0, "error": "no available engine"})

    def _force_due():
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT seq, payload FROM events WHERE repo_key=?",
                            (REPO,)).fetchone()
            payload = json.loads(row[1])
            payload["_not_before"] = "2000-01-01T00:00:00+00:00"
            c.execute("UPDATE events SET payload=? WHERE seq=?",
                      (json.dumps(payload, ensure_ascii=False), row[0]))
            db.commit()
            return row[0]

    seq = _force_due()
    for _ in range(4):
        reconcile_queued_a2a(REPO)
        _force_due()

    status, _payload = _event_row(seq)
    assert status == "failed", "重试耗尽必须留痕终态"
    assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "pending_review"


def _force_recheck_due(seq, repo_key=REPO):
    """把持久化重排事件的 `_not_before` 置为已到点（模拟巡检时刻到达）。"""
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT payload FROM events WHERE seq=?", (int(seq),)).fetchone()
        payload = json.loads(row[0] or "{}")
        payload["_not_before"] = "2000-01-01T00:00:00+00:00"
        c.execute("UPDATE events SET payload=? WHERE seq=?",
                  (json.dumps(payload, ensure_ascii=False), int(seq)))
        db.commit()
    return seq


def _queued_recheck_seq():
    with db_lock:
        row = db.cursor().execute(
            "SELECT seq FROM events WHERE repo_key=? AND status='queued' "
            "ORDER BY seq DESC LIMIT 1", (REPO,)).fetchone()
    return int(row[0]) if row else 0


def _handoff_event(seq, payload, target="coding", node_id="org_dispatch"):
    from src.api.event_bus import GrapeEvent, EventDomain
    return GrapeEvent(
        domain=EventDomain.AGENT, type="agent/handoff", repo_key=REPO,
        issue_id=ISSUE_ID, agent_type=target, seq=int(seq),
        payload={"target_agent": target, "node_id": node_id,
                 "scene_id": SCENE_ID, "evt": {"payload": dict(payload or {})}},
    )


def _patch_plugin_async_path(monkeypatch):
    """真实走 plugin/queue_remote 异步分支所需的环境替换（独立 bus + 节点解析）。"""
    from src.api.event_bus import EventBus
    bus = EventBus()
    monkeypatch.setattr("src.api.event_bus.get_event_bus", lambda: bus)
    monkeypatch.setattr(
        "src.domains.runtime.pipeline.event_pipeline.is_plugin_mode", lambda: True)
    monkeypatch.setattr(
        "src.domains.runtime.engine.service.get_execution_mode", lambda: "queue_remote")
    monkeypatch.setattr(
        "src.api.pipeline_config.get_node_by_tag",
        lambda scene_id, agent, repo_key="", user_id=0: {
            "id": "org_aggregate", "tag": agent, "skill": "org-aggregate-cli",
            "mode": "emit"})
    session_store.set_issue_scene(REPO, ISSUE_ID, SCENE_ID, 0)


def test_async_listener_failure_requeues_aggregate_recheck_until_pending_review(monkeypatch):
    """Critical：plugin/queue_remote 提前返回 async ok 后，listener 的真实派发失败
    必须走聚合重排（退避→耗尽转 pending_review），而不是直接 _fail_event 断链。"""
    monkeypatch.setenv("GRAPE_ORG_AGGREGATE_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(
        "src.domains.runtime.engine.service._start_timer",
        lambda delay, fn, args: object())
    session_store.set_issue_stage(REPO, ISSUE_ID, "qa", "qa")
    assert _schedule_aggregate_recheck(_task("qa"), TRANSITION_AGGREGATE, SCENE_ID, 1)
    seq = _queued_recheck_seq()
    assert seq, "前置：必须存在持久化 queued 重排事件"

    _patch_plugin_async_path(monkeypatch)
    async_results = []
    _real_run = a2a_dispatch.run_a2a_dispatch

    def _run_and_capture(*a, **kw):
        r = _real_run(*a, **kw)
        async_results.append(r)
        return r

    monkeypatch.setattr(a2a_dispatch, "run_a2a_dispatch", _run_and_capture)

    async def _failed_dispatch(**kw):
        return {"status": "failed", "node": kw.get("node_id"), "mode": "emit",
                "error": "no available engine"}

    import src.api.pipeline_executor as _pe
    monkeypatch.setattr(_pe, "dispatch_node", _failed_dispatch)

    attempted = 0
    for _ in range(4):
        _force_recheck_due(seq)
        reconcile_queued_a2a(REPO)
        attempted += 1
        status, payload = _event_row(seq)
        if status == "failed":
            break
        assert status == "queued", f"listener 失败后应退避重排，实际 status={status!r}"
        assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "qa", \
            "重排期间 stage 必须仍停在当前聚合节点"
        assert int(payload.get("aggregate_attempt") or 0) == attempted

    assert async_results and async_results[0].get("async") is True, \
        "必须真实走 plugin/queue_remote 的 async ok 分支"
    status, payload = _event_row(seq)
    assert status == "failed", "重试耗尽必须留痕终态"
    assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "pending_review", \
        "重试耗尽必须转人工审核，不得静默停在 qa"
    assert int(payload.get("aggregate_attempt") or 0) == 2
    diag = payload.get("_fail_diag") or {}
    assert diag.get("phase") == "aggregate_exhausted"
    assert int(diag.get("attempts") or 0) == 3


def test_async_listener_exception_requeues_aggregate_recheck_until_pending_review(monkeypatch):
    """Critical：listener 抛异常（而非返回 failed）走同一共享收口。

    异常路径不得在收口之后再补写终态——否则刚置回的 queued 会被二次
    _fail_event 覆盖为 failed（终态保护不拦 queued→failed），退避重排与
    耗尽转 pending_review 全部失效。
    """
    monkeypatch.setenv("GRAPE_ORG_AGGREGATE_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(
        "src.domains.runtime.engine.service._start_timer",
        lambda delay, fn, args: object())
    session_store.set_issue_stage(REPO, ISSUE_ID, "qa", "qa")
    assert _schedule_aggregate_recheck(_task("qa"), TRANSITION_AGGREGATE, SCENE_ID, 1)
    seq = _queued_recheck_seq()
    assert seq, "前置：必须存在持久化 queued 重排事件"

    _patch_plugin_async_path(monkeypatch)
    async_results = []
    _real_run = a2a_dispatch.run_a2a_dispatch

    def _run_and_capture(*a, **kw):
        r = _real_run(*a, **kw)
        async_results.append(r)
        return r

    monkeypatch.setattr(a2a_dispatch, "run_a2a_dispatch", _run_and_capture)

    async def _raising_dispatch(**kw):
        raise RuntimeError("no available engine (listener boom)")

    import src.api.pipeline_executor as _pe
    monkeypatch.setattr(_pe, "dispatch_node", _raising_dispatch)

    attempted = 0
    for _ in range(4):
        _force_recheck_due(seq)
        reconcile_queued_a2a(REPO)
        attempted += 1
        status, payload = _event_row(seq)
        if status == "failed":
            break
        assert status == "queued", \
            f"listener 抛异常后应退避重排（不得被二次 _fail_event 覆盖），实际 status={status!r}"
        assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "qa", \
            "重排期间 stage 必须仍停在当前聚合节点"
        assert int(payload.get("aggregate_attempt") or 0) == attempted

    assert async_results and async_results[0].get("async") is True, \
        "必须真实走 plugin/queue_remote 的 async ok 分支"
    status, payload = _event_row(seq)
    assert status == "failed", "重试耗尽必须留痕终态"
    assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "pending_review", \
        "重试耗尽必须转人工审核，不得静默停在 qa"
    assert int(payload.get("aggregate_attempt") or 0) == 2
    diag = payload.get("_fail_diag") or {}
    assert diag.get("code") == "a2a_dispatch_exception"
    assert diag.get("phase") == "aggregate_exhausted"
    assert int(diag.get("attempts") or 0) == 3


def test_listener_blocked_does_not_advance_auto_advance_stage(monkeypatch):
    """Required：dispatch_node=blocked 未实际派发，绝不能推进 stage_guard。"""
    import asyncio

    session_store.set_issue_stage(REPO, ISSUE_ID, "issue", "issue")
    _patch_transition(monkeypatch, TRANSITION_INTAKE)
    captured = {}

    def _fake_dispatch(repo_key, seq, *a, **kw):
        captured["seq"] = seq
        return {"status": "ok", "seq": seq, "async": True}

    monkeypatch.setattr(a2a_dispatch, "run_a2a_dispatch", _fake_dispatch)
    out = _auto_advance_after_complete(_task("issue"))
    assert out["action"] == "a2a"
    seq = captured["seq"]
    _status, payload = _event_row(seq)

    async def _blocked_dispatch(**kw):
        return {"status": "blocked", "node": kw.get("node_id"),
                "mode": "waterfall", "reason": "intercepted"}

    import src.api.pipeline_executor as _pe
    monkeypatch.setattr(_pe, "dispatch_node", _blocked_dispatch)
    asyncio.run(a2a_dispatch._on_handoff_dispatch(_handoff_event(seq, payload)))

    assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "issue", \
        "blocked 未派发，stage 不得指向下游"
    status, payload = _event_row(seq)
    assert status == "failed", "未确认派发的交办必须留痕，避免僵尸 running"
    assert (payload.get("_fail_diag") or {}).get("code") == "a2a_dispatch_blocked"


def test_listener_dispatched_advances_auto_advance_stage(monkeypatch):
    """回归：确认 dispatched 后才推进 stage_guard（异步路径正向闭环）。"""
    import asyncio

    session_store.set_issue_stage(REPO, ISSUE_ID, "issue", "issue")
    _patch_transition(monkeypatch, TRANSITION_INTAKE)
    captured = {}

    def _fake_dispatch(repo_key, seq, *a, **kw):
        captured["seq"] = seq
        return {"status": "ok", "seq": seq, "async": True}

    monkeypatch.setattr(a2a_dispatch, "run_a2a_dispatch", _fake_dispatch)
    _auto_advance_after_complete(_task("issue"))
    seq = captured["seq"]
    _status, payload = _event_row(seq)

    async def _ok_dispatch(**kw):
        return {"status": "dispatched", "node": kw.get("node_id"), "mode": "emit"}

    import src.api.pipeline_executor as _pe
    monkeypatch.setattr(_pe, "dispatch_node", _ok_dispatch)
    asyncio.run(a2a_dispatch._on_handoff_dispatch(_handoff_event(seq, payload)))

    assert session_store.get_issue_stage(REPO, ISSUE_ID)["stage"] == "coding"


def test_patrol_ignores_failed_handoff_event():
    """Issue #2118 复审：failed 的 a2a_event 不构成「已交棒证据」，断链仍会被巡检发现。"""
    append_event(repo_key=REPO, event_type="a2a_event", title="failed handoff",
                 issue_id=ISSUE_ID, agent_type="coding", payload={},
                 status="failed", idempotency_key="l1-ignore-failed-2118")

    assert l1_event_exists(REPO, ISSUE_ID, "2000-01-01 00:00:00", "coding") is False
