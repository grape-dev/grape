"""Issue #2270: 组织下派节点必须等批次终态后才触发聚合节点。

覆盖 _auto_advance_after_complete 对 org_dispatch 节点的等待语义：
- 节点 id=org_dispatch 时返回 wait_for_dispatch，不自动 A2A 到 org_aggregate；
- 不修改 issue stage（保持 coding），由批次终态钩子负责推进 qa。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.engine import service


def test_org_dispatch_node_waits_for_terminal_batch(monkeypatch):
    calls = {"set_stage": [], "a2a": []}

    monkeypatch.setattr(
        "src.api.session_store.get_issue_stage",
        lambda repo_key, issue_id: {"stage": "coding"})
    monkeypatch.setattr(
        "src.api.session_store.get_issue_scene",
        lambda repo_key, issue_id: ("custom-org-2270", 0))
    monkeypatch.setattr(
        "src.api.pipeline_config.get_pipeline_transition",
        lambda scene_id, agent_role, repo_key="": {
            "node": {"id": "org_dispatch", "tag": "coding", "next": "org_aggregate"},
            "next_role": "qa",
            "terminal": False,
            "handoff": "a2a",
        })
    monkeypatch.setattr(
        "src.api.session_store.set_issue_stage",
        lambda *a, **kw: calls["set_stage"].append((a, kw)))
    monkeypatch.setattr(
        service, "_create_auto_advance_a2a",
        lambda *a, **kw: calls["a2a"].append((a, kw)) or 1)

    out = service._auto_advance_after_complete({
        "repo_key": "gitcode:issue2270/org",
        "issue_id": "#7",
        "agent_type": "coding",
    })

    assert out.get("action") == "wait_for_dispatch", out
    assert not calls["a2a"], "org_dispatch 不得自动 A2A 到聚合节点"
    assert not calls["set_stage"], "等待期间不得改 stage"
