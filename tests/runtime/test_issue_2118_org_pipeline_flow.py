# -*- coding: utf-8 -*-
"""Issue #2118 复审修复：组织节点 A2A / 建单预校验 / 自动推进 / 模板隔离。

覆盖：
- A2A 节点校验按 id 或 tag 命中组织节点（org_* / issue|coding|qa）。
- 阶段推进表同时提供 id 与 tag 别名。
- 建单前 scene_precheck 对 sid=0 的 org-* 技能复用官方内置包门禁。
- 组织场景真实建单走通（不触发 skill_unresolvable）。
- complete_task 按场景 next/handoff 自动推进（stage 未变时补 A2A）；
  已显式交办（stage 已变）不重复派发；末节点置 pending_review。
- org 官方模板不向普通仓库解析。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.api import scene_precheck
import src.api.pipeline_config as pipeline_config
import src.api.session_store as session_store
import src.domains.runtime.engine.service as engine_service
from src.domains.runtime.engine.service import _auto_advance_after_complete
from src.domains.runtime.pipeline.event_pipeline import (
    _org_scene_entry_role,
    _resolve_issue_opened_agent,
)
from src.domains.runtime.routes.webhook import _scene_node_check, _scene_node_order


ORG_REPO = "gitcode:issue2118/org-repo"
NORMAL_REPO = "gitcode:issue2118/normal-repo"
SCENE_ID = "custom-org-issue2118-flow"

ORG_NODES = [
    {
        "id": "org_intake", "tag": "issue", "skill": "org-intake-cli",
        "mode": "emit", "next": "org_dispatch", "handoff": "a2a",
        "skip": [], "enabled": True, "auto_advance": True, "engine_runtime_ids": [],
    },
    {
        "id": "org_dispatch", "tag": "coding", "skill": "org-dispatch-cli",
        "mode": "emit", "next": "org_aggregate", "handoff": "a2a",
        "skip": [], "enabled": True, "auto_advance": True, "engine_runtime_ids": [],
    },
    {
        "id": "org_aggregate", "tag": "qa", "skill": "org-aggregate-cli",
        "mode": "emit", "next": "done", "handoff": "complete",
        "skip": [], "enabled": True, "auto_advance": True, "engine_runtime_ids": [],
    },
]


def _patch_scene(monkeypatch, nodes=None, scene_id=SCENE_ID, scene_type="org"):
    cfg = {"scene_id": scene_id, "scene_type": scene_type, "nodes": nodes or ORG_NODES}

    def _fake(scene_id_arg="default", repo_key="", **kw):
        return cfg

    monkeypatch.setattr(pipeline_config, "get_pipeline_config", _fake)
    monkeypatch.setattr(session_store, "get_issue_scene",
                        lambda repo_key, issue: (scene_id, 0))
    return cfg


# ---------------------------------------------------------------- A2A nodes

def test_scene_node_check_matches_org_tag(monkeypatch):
    _patch_scene(monkeypatch)

    for role, node_id in (("issue", "org_intake"),
                          ("coding", "org_dispatch"),
                          ("qa", "org_aggregate")):
        ck = _scene_node_check(ORG_REPO, 42, role)
        assert ck["in_scene"] is True, role
        assert ck["node"]["id"] == node_id

    assert _scene_node_check(ORG_REPO, 42, "sre")["in_scene"] is False
    assert _scene_node_check(ORG_REPO, 42, "org_dispatch")["in_scene"] is True


def test_scene_node_order_exposes_id_and_tag_aliases(monkeypatch):
    _patch_scene(monkeypatch)

    order = _scene_node_order(ORG_REPO, 42)

    # tag 键用于 A2A 阶段推进（stage 记录的是 tag）
    assert order["issue"] == "coding"
    assert order["coding"] == "qa"
    assert order["qa"] == "done"
    # id 键保留兼容，值同样归一为下一节点角色
    assert order["org_intake"] == "coding"
    assert order["org_dispatch"] == "qa"
    assert order["org_aggregate"] == "done"


def test_org_scene_entry_role_bootstraps_first_node(monkeypatch):
    _patch_scene(monkeypatch)

    evt = {"issue_id": "#42", "payload": {"issue": {"number": 42}}}
    assert _org_scene_entry_role(ORG_REPO, evt) == "issue"

    # 普通场景保持 vod 启动
    _patch_scene(monkeypatch, nodes=ORG_NODES, scene_type="standard")
    assert _org_scene_entry_role(NORMAL_REPO, evt) == ""


def test_issue_opened_waits_scene_binding_before_org_routing(monkeypatch):
    """issue_opened 不能在 set_issue_scene 落库前判定：等待后按 org 首节点启动。"""
    cfg = {"scene_id": SCENE_ID, "scene_type": "org", "nodes": ORG_NODES}
    monkeypatch.setattr(pipeline_config, "get_pipeline_config", lambda *a, **kw: cfg)
    _seq = iter(["", "", SCENE_ID])
    monkeypatch.setattr(session_store, "get_issue_scene",
                        lambda *a, **kw: (next(_seq), 0))

    evt = {"issue_id": "#42", "payload": {"issue": {"number": 42}}}
    agent, scene, is_recommend = _resolve_issue_opened_agent(
        ORG_REPO, evt, attempts=3, interval=0)

    assert agent == "issue"
    assert scene == SCENE_ID
    assert is_recommend is False


def test_issue_opened_without_binding_stays_vod(monkeypatch):
    """无场景绑定（普通 Issue）保持 vod 路由与推荐模式。"""
    monkeypatch.setattr(session_store, "get_issue_scene", lambda *a, **kw: ("", 0))

    evt = {"issue_id": "#43", "payload": {"issue": {"number": 43}}}
    agent, scene, is_recommend = _resolve_issue_opened_agent(
        NORMAL_REPO, evt, attempts=2, interval=0)

    assert agent == "vod"
    assert scene == ""
    assert is_recommend is True


# ------------------------------------------------------------ precheck gate

def _precheck_nodes():
    return [dict(n) for n in ORG_NODES]


def test_scene_precheck_reuses_official_skill_bundle(monkeypatch):
    _patch_scene(monkeypatch, nodes=_precheck_nodes())
    import src.domains.mgmt.user.db as udb
    monkeypatch.setattr(udb, "resolve_skill_id", lambda *a, **kw: 0)

    out = scene_precheck.precheck_scene(SCENE_ID, ORG_REPO, {"username": "me"})

    assert out["ok"] is True, out.get("problems")
    assert not any(p["reason_code"] == "skill_unresolvable" for p in out["problems"])


def test_scene_precheck_still_blocks_unknown_skill(monkeypatch):
    nodes = _precheck_nodes()
    nodes[0]["skill"] = "ghost-skill"
    _patch_scene(monkeypatch, nodes=nodes)
    import src.domains.mgmt.user.db as udb
    monkeypatch.setattr(udb, "resolve_skill_id", lambda *a, **kw: 0)

    out = scene_precheck.precheck_scene(SCENE_ID, ORG_REPO, {"username": "me"})

    assert out["ok"] is False
    assert any(p["reason_code"] == "skill_unresolvable" for p in out["problems"])


# ------------------------------------------------------------- real create

def test_org_scene_real_issue_creation_passes_precheck(monkeypatch):
    """真实走 create_task_via_issue：预校验不 mock，org 技能靠内置包放行。"""
    import src.api.issues_center as ic

    _patch_scene(monkeypatch)
    monkeypatch.setattr(ic, "_find_open_issue_by_title", lambda *a, **kw: None)
    monkeypatch.setattr(ic, "_post_gitcode_issue", lambda *a, **kw: {"number": 2188})
    # D21/S32 收口后建单走 VcsApiClient.create_issue（_post_gitcode_issue 仅存量委托）
    from src.domains.runtime.vcs.api_client import VcsApiClient
    monkeypatch.setattr(VcsApiClient, "create_issue",
                        lambda self, title, body="", assignee="": {"number": 2188})
    monkeypatch.setattr(ic, "ensure_issue_task", lambda *a, **kw: 2188)
    monkeypatch.setattr(ic, "_attach_list", lambda *a, **kw: [])
    captured = {}
    monkeypatch.setattr(
        "src.domains.runtime.pipeline.event_pipeline.run_pipeline",
        lambda *a, **kw: captured.setdefault("called", True) or {"seq": 1},
    )

    body = ic.TaskCreateBody(repo_key=ORG_REPO, title="组织需求", description="下派到两个仓库",
                             scene_id=SCENE_ID)
    user = {"id": 22118, "username": "issue2118-owner", "auth_type": "internal",
            "token": "internal-token"}

    result = asyncio.run(ic.create_task_via_issue(body, user=user))

    assert result["issue_number"] == 2188
    assert result["created"] is True
    assert captured.get("called") is True


# -------------------------------------------------------- auto advancement

def _patch_advance(monkeypatch, *, stage, transition, events, stages, stage_sets,
                   dispatch_result=None):
    monkeypatch.setattr(session_store, "get_issue_stage",
                        lambda repo_key, issue_id: {"stage": stage, "owner_agent": stage})
    monkeypatch.setattr(session_store, "get_issue_scene",
                        lambda repo_key, issue_id: (SCENE_ID, 0))
    monkeypatch.setattr(session_store, "set_issue_stage",
                        lambda repo_key, issue_id, st, owner, **kw: stage_sets.append((st, owner)))
    monkeypatch.setattr(pipeline_config, "get_pipeline_transition",
                        lambda *a, **kw: transition)
    import src.api.event_store as es
    monkeypatch.setattr(es, "append_event", lambda **kw: events.append(kw) or 777)
    import src.api.a2a_dispatch as ad
    dispatched = []
    _result = dispatch_result or {"status": "ok", "seq": 777}
    monkeypatch.setattr(ad, "run_a2a_dispatch",
                        lambda *a, **kw: dispatched.append((a, kw)) or _result)
    import src.api.issues_center as ic
    monkeypatch.setattr(ic, "_get_issue_title", lambda *a, **kw: "组织需求")
    return dispatched


def _task(agent="issue"):
    return {"id": "t_2118", "repo_key": ORG_REPO, "issue_id": "#42",
            "agent_type": agent, "event_seq": 100, "account": "issue2118-owner"}


def test_auto_advance_creates_a2a_when_skill_did_not_handoff(monkeypatch):
    events, stages, stage_sets = [], [], []
    dispatched = _patch_advance(
        monkeypatch, stage="issue",
        transition={"node": ORG_NODES[0], "next_node": ORG_NODES[1],
                    "next_id": "org_dispatch", "next_role": "coding",
                    "terminal": False, "handoff": "a2a"},
        events=events, stages=stages, stage_sets=stage_sets)

    out = _auto_advance_after_complete(_task("issue"))

    assert out["action"] == "a2a"
    assert out["next_role"] == "coding"
    assert events and events[0]["agent_type"] == "coding"
    assert events[0]["payload"]["auto_advance"] is True
    assert stage_sets == [("coding", "coding")]
    assert dispatched and dispatched[0][0][1] == 777


def _plain_node(**kw):
    node = {"id": "qa", "skill": "qa-cli", "mode": "emit", "next": "sre",
            "handoff": "a2a", "skip": [], "enabled": True}
    node.update(kw)
    return node


def _transition_to(node, next_role="sre", next_id="sre", terminal=False):
    return {"node": node, "next_node": {"id": next_id} if not terminal else None,
            "next_id": next_id, "next_role": next_role,
            "terminal": terminal, "handoff": node.get("handoff") or "a2a"}


def test_auto_advance_requires_node_opt_in(monkeypatch):
    """Issue #3688：节点未声明 auto_advance → 不兜底派发（回到技能自行交办语义）。"""
    events, stages, stage_sets = [], [], []
    dispatched = _patch_advance(
        monkeypatch, stage="qa",
        transition=_transition_to(_plain_node()),
        events=events, stages=stages, stage_sets=stage_sets)

    out = _auto_advance_after_complete(_task("qa"))

    assert out["action"] == "none"
    assert out["reason"] == "auto_advance_not_declared"
    assert events == [] and stage_sets == [] and dispatched == []


def test_auto_advance_runs_when_node_opts_in(monkeypatch):
    """节点显式 auto_advance=True 时保留 #2118 兜底派发。"""
    events, stages, stage_sets = [], [], []
    node = _plain_node(auto_advance=True)
    dispatched = _patch_advance(
        monkeypatch, stage="qa", transition=_transition_to(node),
        events=events, stages=stages, stage_sets=stage_sets)

    out = _auto_advance_after_complete(_task("qa"))

    assert out["action"] == "a2a"
    assert out["next_role"] == "sre"
    assert events and events[0]["agent_type"] == "sre"
    assert events[0]["payload"]["auto_advance"] is True
    assert stage_sets == [("sre", "sre")]
    assert dispatched


def test_auto_advance_accepts_string_flag(monkeypatch):
    """场景编辑页/接口可能以字符串下发，"true"/1 同样视为开启。"""
    events, stages, stage_sets = [], [], []
    node = _plain_node(auto_advance="true")
    _patch_advance(monkeypatch, stage="qa", transition=_transition_to(node),
                   events=events, stages=stages, stage_sets=stage_sets)

    out = _auto_advance_after_complete(_task("qa"))

    assert out["action"] == "a2a"


def test_terminal_pending_review_is_not_gated_by_opt_in(monkeypatch):
    """末节点置 pending_review 属 Issue #656 既有语义，不受 auto_advance 开关影响。"""
    events, stages, stage_sets = [], [], []
    node = _plain_node(id="sre", skill="sre-cli", next="done", handoff="complete")
    _patch_advance(monkeypatch, stage="sre",
                   transition=_transition_to(node, next_role="done",
                                             next_id="done", terminal=True),
                   events=events, stages=stages, stage_sets=stage_sets)

    out = _auto_advance_after_complete(_task("sre"))

    assert out["action"] == "pending_review"
    assert stage_sets == [("pending_review", "sre")]


def test_auto_advance_skips_when_stage_already_advanced(monkeypatch):
    events, stages, stage_sets = [], [], []
    dispatched = _patch_advance(
        monkeypatch, stage="coding",
        transition={"node": ORG_NODES[0], "next_node": ORG_NODES[1],
                    "next_id": "org_dispatch", "next_role": "coding",
                    "terminal": False, "handoff": "a2a"},
        events=events, stages=stages, stage_sets=stage_sets)

    out = _auto_advance_after_complete(_task("issue"))

    assert out["action"] == "none"
    assert out["reason"] == "stage_already_advanced"
    assert events == [] and stage_sets == [] and dispatched == []


def test_auto_advance_terminal_node_goes_pending_review(monkeypatch):
    events, stages, stage_sets = [], [], []
    dispatched = _patch_advance(
        monkeypatch, stage="qa",
        transition={"node": ORG_NODES[2], "next_node": None,
                    "next_id": "done", "next_role": "done",
                    "terminal": True, "handoff": "complete"},
        events=events, stages=stages, stage_sets=stage_sets)

    out = _auto_advance_after_complete(_task("qa"))

    assert out["action"] == "pending_review"
    assert stage_sets == [("pending_review", "qa")]
    assert events == [] and dispatched == []


def test_auto_advance_aggregate_remaining_schedules_recheck(monkeypatch):
    """聚合未全终态：不提前 pending_review，安排下一轮聚合。"""
    events, stages, stage_sets = [], [], []
    _patch_advance(
        monkeypatch, stage="qa",
        transition={"node": ORG_NODES[2], "next_node": None,
                    "next_id": "done", "next_role": "done",
                    "terminal": True, "handoff": "complete"},
        events=events, stages=stages, stage_sets=stage_sets)
    timers = []
    monkeypatch.setattr(engine_service, "_start_timer",
                        lambda delay, fn, args: timers.append((delay, fn, args)) or object())

    out = _auto_advance_after_complete(
        _task("qa"),
        output={"remaining": [{"repo_key": "gitcode:org/repo-b", "status": "running"}],
                "next_step": "a2a-qa"})

    assert out["action"] == "recheck"
    assert out["round"] == 1
    assert stage_sets == [], "未终态不得提前 pending_review"
    assert len(timers) == 1
    assert timers[0][1] is engine_service._run_aggregate_recheck
    assert timers[0][2][3] == 1


def test_aggregate_recheck_fast_path_dispatches_persisted_seq(monkeypatch):
    """定时器快派：把已持久化的 queued 重排事件按 seq 推送（真源仍是持久化事件）。"""
    events, stages, stage_sets = [], [], []
    _patch_advance(
        monkeypatch, stage="qa",
        transition={"node": ORG_NODES[2], "next_node": None,
                    "next_id": "done", "next_role": "done",
                    "terminal": True, "handoff": "complete"},
        events=events, stages=stages, stage_sets=stage_sets)
    import src.api.patrol as patrol
    monkeypatch.setattr(patrol, "issue_terminal", lambda *a, **kw: "")
    import src.domains.runtime.pipeline.event_pipeline as ep
    captured = {}
    monkeypatch.setattr(
        ep, "dispatch_delayed_recheck",
        lambda seq: captured.setdefault("seq", seq) or True)

    engine_service._run_aggregate_recheck(
        _task("qa"),
        {"node": ORG_NODES[2], "next_node": None, "next_id": "done",
         "next_role": "done", "terminal": True, "handoff": "complete"},
        SCENE_ID, 2, 555)

    assert captured.get("seq") == 555


def test_auto_advance_dispatch_failure_keeps_stage(monkeypatch):
    """自动推进派发 failed：不回写 stage 到未运行的下游，并显式失败留痕。"""
    events, stages, stage_sets = [], [], []
    dispatched = _patch_advance(
        monkeypatch, stage="issue",
        transition={"node": ORG_NODES[0], "next_node": ORG_NODES[1],
                    "next_id": "org_dispatch", "next_role": "coding",
                    "terminal": False, "handoff": "a2a"},
        events=events, stages=stages, stage_sets=stage_sets,
        dispatch_result={"status": "failed", "seq": 0, "error": "no available engine"})
    fails = []
    import src.domains.runtime.pipeline.event_pipeline as ep
    monkeypatch.setattr(ep, "_fail_event", lambda seq, msg, **kw: fails.append((seq, msg)))

    out = _auto_advance_after_complete(_task("issue"))

    assert out["action"] == "none"
    assert out["reason"] == "dispatch_failed"
    assert stage_sets == [], "派发失败不得先推进 stage"
    assert events, "事件已创建，需置失败留痕"
    assert fails and fails[0][0] == 777 and dispatched


# --------------------------------------------------------- pipeline dispatch

def test_pipeline_executor_resolves_org_node_by_tag(monkeypatch):
    import src.api.pipeline_executor as pe

    monkeypatch.setattr(pe, "get_pipeline_config",
                        lambda scene_id, repo_key="": {"scene_id": SCENE_ID,
                                                      "scene_type": "org",
                                                      "nodes": ORG_NODES})
    seen = {}

    async def _fake_emit(node, *a, **kw):
        seen["node"] = node
        return {"status": "dispatched", "node": node["id"], "mode": "emit"}

    monkeypatch.setattr(pe, "_dispatch_emit", _fake_emit)

    async def _run():
        return await pe.dispatch_node(
            scene_id=SCENE_ID, node_id="coding", repo_key=ORG_REPO,
            issue_id="#42", event_seq=1, evt={},
        )

    out = asyncio.run(_run())
    assert out["status"] == "dispatched"
    assert seen["node"]["id"] == "org_dispatch"
    assert seen["node"]["tag"] == "coding"


# ------------------------------------------------------- template isolation

def test_org_official_template_not_resolved_for_normal_repo(monkeypatch):
    monkeypatch.setattr(pipeline_config, "_repo_is_org_repo", lambda repo_key: False)
    pipeline_config.invalidate_scene_cache("org", NORMAL_REPO)

    assert pipeline_config.get_pipeline_config("org", repo_key=NORMAL_REPO) is None


def test_org_template_resolves_for_org_repo(monkeypatch):
    monkeypatch.setattr(pipeline_config, "_repo_is_org_repo", lambda repo_key: True)
    pipeline_config.invalidate_scene_cache("org", ORG_REPO)

    cfg = pipeline_config.get_pipeline_config("org", repo_key=ORG_REPO)

    assert cfg is not None
    assert [n["id"] for n in cfg["nodes"]] == [
        "org_intake", "org_dispatch", "org_aggregate",
    ]


# --------------------------------------------- 本地 direct 收尾钩子对齐

def test_run_auto_advance_hook_assembles_task_and_parses_output(monkeypatch):
    """本地收尾入口：任务上下文装配 + 结构化产出解析后交给同一判定函数。"""
    captured = {}

    def _fake(task, output=None, result_text=""):
        captured.update(task=task, output=output, result_text=result_text)
        return {"action": "a2a", "next_role": "qa", "seq": 901}

    monkeypatch.setattr(engine_service, "_auto_advance_after_complete", _fake)

    out = engine_service.run_auto_advance_hook(
        "t_local", ORG_REPO, "#42", "coding", 123,
        '{"actions": ["x"], "conclusion": "ok"}')

    assert out == {"action": "a2a", "next_role": "qa", "seq": 901}
    assert captured["task"] == {"id": "t_local", "repo_key": ORG_REPO,
                                "issue_id": "#42", "agent_type": "coding",
                                "event_seq": 123}
    assert captured["output"]["conclusion"] == "ok"


def test_local_executors_call_auto_advance_hook():
    """本地 direct 两条收尾路径（event_pipeline / direct_executor）都必须调用钩子。"""
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    for rel in ("src/domains/runtime/pipeline/event_pipeline.py",
                "src/domains/runtime/engine/direct_executor.py"):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            src = f.read()
        assert "run_auto_advance_hook(" in src, f"{rel} 收尾未调用 run_auto_advance_hook"


def test_official_market_lookup_excludes_org_scope(monkeypatch):
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM scene_market WHERE name='org-iso-2118'")
        c.execute(
            "INSERT INTO scene_market (name, display_name, scope, source, nodes_json, is_active) "
            "VALUES (?,?,?,?,?,?)",
            ("org-iso-2118", "org-iso-2118", "org", "official", "[]", 1))
        db.commit()
    try:
        assert pipeline_config._load_official_scene_from_market("org-iso-2118") is None
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM scene_market WHERE name='org-iso-2118'")
            db.commit()


# ---------------------------------------------------- Issue #3688 opt-in
def test_org_official_template_declares_auto_advance():
    """组织官方模板必须显式声明 auto_advance，否则 #2118 兜底会在组织链条上失效。"""
    from src.domains.mgmt.pipeline_config import ORG_SCENE_NODES
    import json
    import os

    assert all(node.get("auto_advance") is True for node in ORG_SCENE_NODES)

    cfg_path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "configs", "pipelines.json")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    org_nodes = cfg["scenes"]["org"]["nodes"]
    assert all(node.get("auto_advance") is True for node in org_nodes)
    # 标准模板保持缺省（回到技能自行交办语义），交付类节点不得隐式自动推进
    default_nodes = cfg["scenes"]["default"]["nodes"]
    assert all(node.get("auto_advance") is None for node in default_nodes)
