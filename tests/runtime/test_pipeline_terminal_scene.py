# -*- coding: utf-8 -*-
"""任意场景的收口判定：末节点按角色（id 或 tag）动态解析，不写死 qa/sre。

覆盖：
- 末节点判定按 tag 命中（组织场景 id=org_aggregate / tag=qa）；
- 组织下派节点按 skill/下一节点 skill 识别，不依赖 id 一定叫 org_dispatch；
- 组织聚合节点在自定义场景中按 skill 兜底解析。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest


ORG_NODES = [
    {"id": "intake_x", "tag": "issue", "skill": "org-intake-cli",
     "next": "dispatch_x", "handoff": "a2a"},
    {"id": "dispatch_x", "tag": "coding", "skill": "org-dispatch-cli",
     "next": "agg_x", "handoff": "a2a"},
    {"id": "agg_x", "tag": "qa", "skill": "org-aggregate-cli",
     "next": "done", "handoff": "complete"},
]


def _patch_scene(monkeypatch, nodes=ORG_NODES, stage="qa"):
    import src.api.session_store as ss
    import src.domains.mgmt.public as mgmt_public

    monkeypatch.setattr(ss, "get_issue_stage",
                        lambda repo, iss: {"stage": stage})
    monkeypatch.setattr(ss, "get_issue_scene", lambda repo, iss: ("org-custom", 0))
    monkeypatch.setattr(mgmt_public, "get_pipeline_config",
                        lambda scene_id="", repo_key="", **kw: {"nodes": nodes})


@pytest.mark.parametrize("agent,expected", [
    ("qa", True),       # 末节点（tag 命中 id=agg_x）
    ("coding", False),  # 中间节点
    ("issue", False),
])
def test_last_node_check_matches_by_tag(monkeypatch, agent, expected):
    from src.domains.runtime import issues_center

    _patch_scene(monkeypatch, stage=agent)

    assert issues_center._pipeline_last_node_check(
        "gitcode:acme/org-repo", 1, agent) is expected


def test_last_node_check_custom_terminal_node_name(monkeypatch):
    """自定义场景的末节点可以叫任何名字，只要 next=done。"""
    from src.domains.runtime import issues_center

    nodes = [
        {"id": "build", "tag": "coding", "next": "release"},
        {"id": "release", "tag": "sre", "next": "done"},
    ]
    _patch_scene(monkeypatch, nodes=nodes, stage="sre")

    assert issues_center._pipeline_last_node_check(
        "gitcode:acme/svc", 2, "sre") is True


def test_org_dispatch_node_is_recognized_by_skill(monkeypatch):
    """下派节点换 id 但保持 org-dispatch-cli 语义时，仍必须等批次终态。"""
    import src.api.pipeline_config as pipeline_config
    import src.api.session_store as session_store
    from src.domains.runtime.engine.service import _auto_advance_after_complete

    monkeypatch.setattr(session_store, "get_issue_stage",
                        lambda repo, iss: {"stage": "coding"})
    monkeypatch.setattr(session_store, "get_issue_scene",
                        lambda repo, iss: ("org-custom", 0))
    monkeypatch.setattr(session_store, "set_issue_stage",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("must not advance stage")))
    monkeypatch.setattr(
        pipeline_config, "get_pipeline_transition",
        lambda *a, **kw: {
            "node": ORG_NODES[1], "next_node": ORG_NODES[2],
            "next_id": "agg_x", "next_role": "qa", "terminal": False,
            "handoff": "a2a",
        })

    out = _auto_advance_after_complete({
        "id": "t_skill", "repo_key": "gitcode:acme/org-repo", "issue_id": "#1",
        "agent_type": "coding", "event_seq": 100,
    })

    assert out["action"] == "wait_for_dispatch"


def test_org_aggregate_node_resolved_by_skill_in_custom_scene(monkeypatch):
    """自定义组织场景把聚合节点改名后，聚合事件仍能解析到 org-aggregate-cli。"""
    import src.api.a2a_dispatch as ad
    import src.api.event_store as event_store
    import src.api.pipeline_config as pipeline_config

    captured: dict = {}
    monkeypatch.setattr(event_store, "find_event_by_idempotency_key",
                        lambda *a, **kw: None)
    monkeypatch.setattr(event_store, "append_event",
                        lambda **kw: (captured.update(kw) or 777))
    monkeypatch.setattr(pipeline_config, "get_pipeline_transition",
                        lambda *a, **kw: {})
    monkeypatch.setattr(pipeline_config, "get_pipeline_config",
                        lambda *a, **kw: {"nodes": ORG_NODES})
    import src.api.session_store as ss
    monkeypatch.setattr(ss, "set_issue_stage", lambda *a, **kw: None)
    dispatch = {"id": "d_custom", "org_repo_key": "gitcode:acme/org-repo",
                "org_issue_number": 1, "org_id": "org-1", "scene_id": "org-custom"}

    res = ad.trigger_org_aggregate(dispatch, [], "needs_review")

    assert res["status"] == "queued" and res["seq"] == 777
    assert captured["agent_type"] == "qa"
    assert captured["payload"]["_a2a_queue"]["dispatch"]["skill_name"] == "org-aggregate-cli"