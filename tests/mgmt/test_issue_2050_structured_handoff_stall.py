"""Issue #2050：结构化 handoff + 平台内建停滞兜底 + 看板停滞态。

覆盖：
1. 结构化 handoff：source_event_seq 字段 / 返回值 ok|failed|rejected 归一；
2. 停滞判据：末段 completed 且无新段、文本缺失时按场景阶段序唯一推导；
3. 历史文本 A2A: 行兼容解析（降级路径，审计日志，含行尾说明）；
4. 看板/详情停滞态与停滞时长字段（排除 admin/assignee 事件）；
5. 契约 schema 校验与全链路回归证据。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.api import patrol as patrol_mod
from src.api import patrol_compensate as pc
from src.api import pipeline_status as ps
from src.domains.runtime.routes.webhook import A2AHandoffBody, _handoff_result_of
from patrol_fakes import FakeDb


def _old(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


# ====== 1. 结构化 handoff ======

def test_handoff_body_accepts_source_event_seq():
    body = A2AHandoffBody(agent="qa", payload={"repository": "o/r", "issue_number": "1"},
                          idempotency_key="handoff:t1:qa", source_event_seq=42)
    assert body.source_event_seq == 42
    assert body.idempotency_key == "handoff:t1:qa"


@pytest.mark.parametrize("status,seq,expected", [
    ("ok", 7, "ok"),
    ("ok", 0, "failed"),
    ("failed", 7, "failed"),
])
def test_handoff_result_classification(status, seq, expected):
    assert _handoff_result_of(status, seq) == expected


def test_handoff_result_skipped_is_rejected():
    assert _handoff_result_of("ok", 0, skipped=True) == "rejected"


# ====== 2. 停滞判据（空文本不再静默） ======

def _scan_db(candidates, texts):
    frag5 = [(r[0], r[1], r[2], r[3], r[4]) for r in candidates]
    return FakeDb([
        ("SELECT kind, task_id FROM grape_t_patrol_findings", []),
        ("FROM agent_tasks WHERE status='completed'", frag5),
        ("id IN", texts),
    ])


def _patched_scan(db):
    return [
        patch.object(patrol_mod, "db", db),
        patch.object(patrol_mod, "db_lock", threading.Lock()),
        patch.object(patrol_mod, "patrol_runtime_config", lambda: {"confirm_minutes": 10}),
        patch.object(patrol_mod, "issue_terminal", lambda *a: ""),
        patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False),
        patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False),
        patch.object(patrol_mod, "_subtask_parent", lambda *a: ""),
    ]


def _run_scan(db, resolve=None, newer=lambda *a: False,
              structured=lambda *a: False):
    patches = _patched_scan(db) + [
        patch.object(patrol_mod, "has_newer_issue_event", newer),
        patch.object(patrol_mod, "structured_handoff_exists", structured),
    ]
    if resolve is not None:
        patches.append(patch.object(patrol_mod, "resolve_handoff_target", resolve))
    for p in patches:
        p.start()
    try:
        return patrol_mod.detect_broken_chains("")
    finally:
        for p in reversed(patches):
            p.stop()


def test_empty_text_last_segment_produces_stall_finding():
    tid = "t-empty"
    cand = [(tid, "gitcode:o/r1", "#2050", "coding", _old(30))]
    db = _scan_db(cand, [(tid, "")])
    out = _run_scan(db, resolve=lambda *a: ("qa", "pipeline"))
    hits = [f for f in out if f["task_id"] == tid]
    assert hits, "末段 completed + 无新段 + 空文本 → 必须进入停滞检测（不再静默）"
    detail = json.loads(hits[0]["detail"])
    assert detail["reason"] == "no_handoff_text"
    assert detail["target_agent"] == "qa" and detail["target_source"] == "pipeline"


def test_newer_segment_suppresses_stall_finding():
    tid = "t-progress"
    cand = [(tid, "gitcode:o/r1", "#2050", "coding", _old(30))]
    db = _scan_db(cand, [(tid, "")])
    out = _run_scan(db, resolve=lambda *a: ("qa", "pipeline"), newer=lambda *a: True)
    assert not [f for f in out if f["task_id"] == tid], "已出现新段（链在推进）不得误报"


def test_text_a2a_candidate_not_suppressed_by_generic_newer_event():
    tid = "t-text"
    cand = [(tid, "gitcode:o/r1", "#2050", "coding", _old(30))]
    db = _scan_db(cand, [(tid, "A2A: → qa")])
    out = _run_scan(db, newer=lambda *a: True)
    hits = [f for f in out if f["task_id"] == tid]
    assert hits, "文本 A2A 未派发时，任意新事件不得把原有可报候选抑制成漏报"
    detail = json.loads(hits[0]["detail"])
    assert detail["target_source"] in ("text", "legacy_text")


def test_structured_handoff_suppresses_stall_even_without_text():
    tid = "t-structured"
    cand = [(tid, "gitcode:o/r1", "#2050", "coding", _old(30))]
    db = _scan_db(cand, [(tid, "")])
    out = _run_scan(db, structured=lambda *a: True)
    assert not [f for f in out if f["task_id"] == tid], \
        "结构化 handoff 引用源段 seq 即视为已交棒（主触发源）"


def test_awaiting_input_is_paused_and_not_broken_chain():
    """QA 复审：awaiting_input 是「等用户」暂停态，不得被判停滞/断链并自动重派发。"""
    from src.api.pipeline_constants import PAUSED_ISSUE_STATUSES
    assert "awaiting_input" in PAUSED_ISSUE_STATUSES
    db = FakeDb([("SELECT status FROM issue", [("awaiting_input",)])])
    with patch.object(patrol_mod, "db", db), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod.issue_terminal("gitcode:o/r1", "#2050") == "awaiting_input"
    tid = "t-awaiting"
    cand = [(tid, "gitcode:o/r1", "#2050", "coding", _old(30))]
    db = _scan_db(cand, [(tid, "")])
    patches = _patched_scan(db) + [
        patch.object(patrol_mod, "issue_terminal", lambda *a: "awaiting_input"),
        patch.object(patrol_mod, "resolve_handoff_target", lambda *a: ("qa", "pipeline")),
        patch.object(patrol_mod, "has_newer_issue_event", lambda *a: False),
        patch.object(patrol_mod, "structured_handoff_exists", lambda *a: False),
    ]
    for p in patches:
        p.start()
    try:
        out = patrol_mod.detect_broken_chains("")
    finally:
        for p in reversed(patches):
            p.stop()
    assert not [f for f in out if f["task_id"] == tid], \
        "awaiting_input 等待用户澄清，不得进入断链检测触发自动重派发"


def test_stall_state_awaiting_input_is_paused():
    out = ps.compute_stall_state([_ev(1, "completed", 30)], "coding", "awaiting_input",
                                 confirm_minutes=10)
    assert out["stalled"] is False
    assert out["stall_state"] == ps.STALL_STATE_PAUSED


def test_recheck_awaiting_input_is_false_positive():
    ctx = pc.CompensateContext(finding_id="f-paused", task_id="t1",
                               repo_key="gitcode:o/r", issue_id="#2050", status="claiming")
    rec = []
    with patch.object(pc, "db", FakeDb([("FROM agent_tasks WHERE id=?", [(_old(60), "coding", "")])])), \
         patch.object(pc, "db_lock", threading.Lock()), \
         patch.object(pc, "_set_finding", lambda fid, st, meta: rec.append((st, meta))), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: "awaiting_input"), \
         patch.object(patrol_mod, "resolve_handoff_target", lambda *a: ("qa", "pipeline")), \
         patch.object(patrol_mod, "structured_handoff_exists", lambda *a: False):
        ok = pc.RecheckHandler().handle(ctx)
    assert ok is False
    assert rec and rec[0][0] == "false_positive", \
        "等待用户澄清时终检应判误报，不得自动补发绕过人工等待"


def test_derive_next_target_from_scene_order():
    nodes = [{"id": "vod"}, {"id": "issue"}, {"id": "coding"}, {"id": "qa"}]
    with patch("src.api.a2a_dispatch_guard.resolve_scene", lambda *a: ("s1", nodes)), \
         patch("src.api.a2a_dispatch_guard.pipeline_order", lambda ns: [n["id"] for n in ns]), \
         patch.object(patrol_mod, "issue_stage_value", lambda *a: "coding"):
        assert patrol_mod.derive_next_target_from_scene("gitcode:o/r", "#1", "coding") == "qa"
        assert patrol_mod.derive_next_target_from_scene("gitcode:o/r", "#1", "qa") == ""


def test_has_newer_issue_event_filters_to_pipeline_segments():
    db = FakeDb([("SELECT created_at FROM events", [(_old(1),)])])
    with patch.object(patrol_mod, "db", db), patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod.has_newer_issue_event("gitcode:o/r", "#1", _old(30)) is True
    sql = " ".join(s for s, _ in db.executed)
    assert "event_type IN" in sql and "admin_action" not in sql, \
        "无新段证据必须限定为流水线 a2a_event，不能读取任意 events 行"
    db = FakeDb([("SELECT created_at FROM events", [(_old(60),)])])
    with patch.object(patrol_mod, "db", db), patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod.has_newer_issue_event("gitcode:o/r", "#1", _old(30)) is False


def test_full_chain_vod_issue_coding_qa_progresses_without_stall():
    nodes = [{"id": "vod"}, {"id": "issue"}, {"id": "coding"}, {"id": "qa"}]
    order = [n["id"] for n in nodes]
    next_of = {"vod": "issue", "issue": "coding", "coding": "qa", "qa": ""}
    for stage in order:
        with patch("src.api.a2a_dispatch_guard.resolve_scene", lambda *a: ("s1", nodes)), \
             patch("src.api.a2a_dispatch_guard.pipeline_order", lambda ns: order), \
             patch.object(patrol_mod, "issue_stage_value", lambda *a, s=stage: s):
            assert patrol_mod.derive_next_target_from_scene(
                "gitcode:o/r", "#1", stage) == next_of[stage]
    chain = [_ev(i, "completed", 60 - i, agent=a) for i, a in
             enumerate(("vod", "issue", "coding", "qa"), start=1)]
    state = ps.compute_stall_state(chain, "done", "completed", confirm_minutes=10)
    assert state["stalled"] is False and state["stall_state"] == ps.STALL_STATE_COMPLETED


# ====== 3. 历史文本 A2A: 行兼容 ======

@pytest.mark.parametrize("line,expected", [
    ("A2A: → qa", "qa"),
    ("A2A: → qa（待确认）", "qa"),
    ("A2A: → qa - 交接", "qa"),
    ("A2A: → qa (cross-pipeline, scene=运行域)", "qa"),
    ("A2A:issue", "issue"),
])
def test_legacy_text_a2a_line_parsed(line, expected):
    rt = json.dumps({"conclusion": "修复完成\n" + line, "issue_summary": "done"})
    assert patrol_mod.extract_legacy_text_handoff(rt) == expected


def test_legacy_text_line_ignored_when_structured_present():
    rt = json.dumps({"next_step": "a2a-sre", "conclusion": "A2A: → qa"})
    target, source = patrol_mod.resolve_handoff_target("gitcode:o/r", "#1", "coding", rt)
    assert (target, source) == ("sre", "text"), "结构化调用优先，历史文本仅降级"


def test_empty_text_derives_pipeline_target_in_recheck():
    """A 方案：场景序推导只作告警建议，绝不进入派发（负责人 11-12 拍板）。"""
    ctx = pc.CompensateContext(finding_id="f1", task_id="t1",
                               repo_key="gitcode:o/r", issue_id="#2050", status="claiming")
    rec = []
    with patch.object(pc, "db", FakeDb([("FROM agent_tasks WHERE id=?", [(_old(60), "coding", "")])])), \
         patch.object(pc, "db_lock", threading.Lock()), \
         patch.object(pc, "_set_finding", lambda fid, st, meta: rec.append((st, meta))), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "resolve_handoff_target", lambda *a: ("qa", "pipeline")), \
         patch.object(patrol_mod, "structured_handoff_exists", lambda *a: False), \
         patch.object(patrol_mod, "has_newer_issue_event", lambda *a: False), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""):
        ok = pc.RecheckHandler().handle(ctx)
    assert ok is False, "场景推导结果不得作为派发依据（猜错就是给错的人派活）"
    assert rec and rec[0][0] == "needs_human", "推导命中也要转人工，不进批处理自愈"
    assert "qa" in rec[0][1], "推导出的目标应保留在告警文案里供人工参考"
    assert ctx.verdict == "pipeline_target_advisory"


def test_text_target_still_dispatchable_in_recheck():
    """护栏只拦 pipeline 推导：agent 已声明目标的文本交棒照旧可补发。"""
    ctx = pc.CompensateContext(finding_id="f2", task_id="t2",
                               repo_key="gitcode:o/r", issue_id="#2050", status="claiming")
    with patch.object(pc, "db", FakeDb([("FROM agent_tasks WHERE id=?", [(_old(60), "coding", "A2A: → qa")])])), \
         patch.object(pc, "db_lock", threading.Lock()), \
         patch.object(pc, "_set_finding", lambda *a: None), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "resolve_handoff_target", lambda *a: ("qa", "text")), \
         patch.object(patrol_mod, "structured_handoff_exists", lambda *a: False), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""):
        ok = pc.RecheckHandler().handle(ctx)
    assert ok is True and ctx.target_agent == "qa", "文本已声明目标 → 仍按原口径补发"


def test_pipeline_advisory_never_reaches_dispatcher():
    """链级证据：推导候选在终检短路，DispatchHandler 一次也不会被调用。"""
    import asyncio
    ctx = pc.CompensateContext(finding_id="f3", task_id="t3",
                               repo_key="gitcode:o/r", issue_id="#2050", status="claiming")
    calls = []

    class _Boom:
        async def handle(self, ctx):
            calls.append("dispatch")
            raise AssertionError("场景推导候选不得进入派发")

    with patch.object(pc, "_load_finding", lambda fid: ctx), \
         patch.object(pc, "db", FakeDb([("FROM agent_tasks WHERE id=?", [(_old(60), "coding", "")])])), \
         patch.object(pc, "db_lock", threading.Lock()), \
         patch.object(pc, "_set_finding", lambda *a: None), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "resolve_handoff_target", lambda *a: ("qa", "pipeline")), \
         patch.object(patrol_mod, "structured_handoff_exists", lambda *a: False), \
         patch.object(patrol_mod, "has_newer_issue_event", lambda *a: False), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""), \
         patch.object(pc, "DispatchHandler", _Boom):
        verdict = asyncio.run(pc.compensate_finding("f3"))
    assert verdict == "pipeline_target_advisory" and not calls


# ====== 4. 看板/详情停滞态 ======

def _ev(seq, status, minutes=30, agent="coding", event_type="a2a_event",
        completed_at=None, created_at=None):
    return {"seq": seq, "status": status, "agent_type": agent,
            "event_type": event_type,
            "created_at": created_at if created_at is not None else _old(minutes),
            "completed_at": completed_at if completed_at is not None else _old(minutes)}


def test_stall_state_stalled_with_duration():
    out = ps.compute_stall_state([_ev(1, "completed", 30)], "coding", "processing",
                                 confirm_minutes=10)
    assert out["stalled"] is True
    assert out["stall_state"] == ps.STALL_STATE_STALLED
    assert out["stalled_minutes"] >= 29


def test_stall_state_done_is_normal_completed():
    out = ps.compute_stall_state([_ev(1, "completed", 30)], "done", "completed")
    assert out["stalled"] is False
    assert out["stall_state"] == ps.STALL_STATE_COMPLETED


def test_stall_state_running_not_stalled():
    out = ps.compute_stall_state([_ev(1, "completed", 30), _ev(2, "running", 1)],
                                 "qa", "processing")
    assert out["stalled"] is False and out["stall_state"] == ps.STALL_STATE_RUNNING


def test_stall_state_ignores_admin_and_assignee_events():
    events = [_ev(1, "completed", 30),
              _ev(2, "completed", 1, agent="", event_type="admin_action"),
              _ev(3, "completed", 1, agent="", event_type="assignee_changed")]
    out = ps.compute_stall_state(events, "coding", "processing", confirm_minutes=10)
    assert out["stalled"] is True, "admin_action/assignee_changed 不得污染末段停滞判定"
    assert out["stalled_minutes"] >= 29


def test_stall_state_missing_completed_at_does_not_fall_back_to_created_at():
    events = [_ev(1, "completed", 30, completed_at="")]
    out = ps.compute_stall_state(events, "coding", "processing", confirm_minutes=10)
    assert out["stalled"] is False and out["stalled_minutes"] == 0, \
        "缺 completed_at 时回退 created_at 会高估停滞时长，应判 unknown"


def test_pipeline_enrich_carries_stall_fields():
    pipeline = {"segments": [{"seq": 1, "status": "completed",
                              "agent": "coding", "event_type": "a2a_event",
                              "completed_at": _old(30),
                              "created_at": _old(30), "output": {}}]}
    ps.enrich_pipeline_business_status(pipeline, "coding", "processing")
    assert pipeline["stalled"] is True
    assert pipeline["stalled_minutes"] >= 29


# ====== 5. 契约与告警通道 ======

def test_contract_schemas_validate_request_and_response():
    jsonschema = pytest.importorskip("jsonschema")
    import os
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(root, "contracts", "a2a_handoff.schema.json"),
              encoding="utf-8") as f:
        request_schema = json.load(f)
    with open(os.path.join(root, "contracts", "a2a_handoff_response.schema.json"),
              encoding="utf-8") as f:
        response_schema = json.load(f)
    jsonschema.validate({"agent": "qa",
                         "payload": {"repository": "o/r", "issue_number": "2050"},
                         "idempotency_key": "handoff:t1:qa",
                         "source_event_seq": 42}, request_schema)
    jsonschema.validate({"status": "ok", "seq": 7, "agent": "qa",
                         "handoff_result": "ok", "source_seq": 42}, response_schema)


def test_run_patrol_once_delivers_stall_finding():
    finding = {"kind": "broken_chain", "task_id": "t1", "event_seq": 0,
               "repo_key": "gitcode:o/r", "issue_id": "#2050",
               "detail": "{}", "suggestion": "补发交棒 → qa"}
    captured = []
    with patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"lookback_mode": "runtime", "confirm_minutes": 10}), \
         patch.object(patrol_mod, "_scan_probe_has_candidates", lambda c: True), \
         patch.object(patrol_mod, "_known_task_ids", lambda: set()), \
         patch.object(patrol_mod, "detect_broken_chains", lambda *a, **k: [finding]), \
         patch.object(patrol_mod, "detect_zombies", lambda *a, **k: []), \
         patch.object(patrol_mod, "derive_stalled_issues", lambda *a: []), \
         patch.object(patrol_mod, "insert_findings_batch",
                      lambda fs: captured.extend(fs) or len(fs)), \
         patch.object(patrol_mod, "cleanup_terminal_findings", lambda *a: 0):
        out = patrol_mod.run_patrol_once("runtime")
    assert captured and captured[0]["task_id"] == "t1"
    assert out["broken"] == 1 and out["inserted"] == 1

# ====== 6. 交棒证据口径：失败事件不算交棒（审查 C4） ======

def _handoff_db(source_seq, event_rows):
    return FakeDb([
        ("FROM agent_tasks WHERE id=?", [(source_seq,)]),
        ("FROM events WHERE repo_key=?", event_rows),
    ])


def _call_handoff(db, task_id="t-evidence"):
    with patch.object(patrol_mod, "db", db), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        return patrol_mod.structured_handoff_exists("gitcode:o/r", "#2050", task_id)


def test_failed_handoff_event_is_not_evidence():
    db = _handoff_db(42, [(json.dumps({"source_seq": 42}), "failed")])
    assert _call_handoff(db) is False, \
        "派发失败的事件不能当成交棒证据，否则停滞被永久抑制（假阴性）"


def test_other_source_seq_is_not_evidence():
    db = _handoff_db(42, [(json.dumps({"source_seq": 7}), "completed")])
    assert _call_handoff(db) is False


@pytest.mark.parametrize("status", ["queued", "dispatched", "running", "completed", "pending"])
def test_live_handoff_event_is_evidence(status):
    db = _handoff_db(42, [(json.dumps({"source_seq": 42}), status)])
    assert _call_handoff(db) is True


def test_cross_pipeline_payload_source_seq_is_evidence():
    payload = {"source_seq": 42, "target_scene_id": "scene-b"}
    db = _handoff_db(42, [(json.dumps(payload), "running")])
    assert _call_handoff(db) is True, \
        "跨流水线 payload 落 source_seq 后必须被认作已交棒（否则合法交棒被判断链）"


# ====== 7. 契约口径对齐（QA 第 5 轮 M1）与详情端点数据源（R4） ======

def _load_schema(name):
    import os
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(root, "contracts", name), encoding="utf-8") as f:
        return json.load(f)


def test_contract_accepts_custom_agent_and_int_issue_number():
    jsonschema = pytest.importorskip("jsonschema")
    request_schema = _load_schema("a2a_handoff.schema.json")
    # 自定义场景节点（nodes=单一事实源）与 int 形态单号都必须过契约校验
    jsonschema.validate({"agent": "my-custom-node",
                         "payload": {"repository": "o/r", "issue_number": 42}},
                        request_schema)
    jsonschema.validate({"agent": "coding",
                         "payload": {"repository": "o/r", "issue_number": "412"}},
                        request_schema)


def test_contract_rejects_invalid_body():
    jsonschema = pytest.importorskip("jsonschema")
    request_schema = _load_schema("a2a_handoff.schema.json")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"payload": {"repository": "o/r", "issue_number": "1"}},
                            request_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"agent": "qa", "payload": {"issue_number": "1"}},
                            request_schema)


def test_issue_detail_events_carry_completed_at():
    """详情端点（_group_events_by_issue）必须取到 completed_at，否则停滞态恒 unknown。"""
    from src.domains.runtime.routes import issues as issues_mod
    from src.kernel import session_manager as sm
    row = (7, "#2050", "a2a_event", "coding", "t", "acc", "sum", "completed",
           "2026-11-12 10:00:00", "", "rt", "sk", "", "2026-11-12 10:30:00")
    db = FakeDb([("FROM events WHERE repo_key IN", [row])])
    with patch.object(sm, "db", db, create=True), \
         patch.object(sm, "db_lock", threading.Lock(), create=True):
        grouped = issues_mod._group_events_by_issue(["gitcode:o/r"], numbers=["2050"])
    ev = grouped["2050"][0]
    assert ev["completed_at"] == "2026-11-12 10:30:00"
    # 值必须能被停滞判定解析（不解析 = 白取一列）
    state = ps.compute_stall_state([ev], "coding", "processing", confirm_minutes=10)
    assert state["stall_state"] in ("grace", "stalled"), state
