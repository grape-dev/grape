"""Issue #2792 切片 1：A2A 前向跳步集合（skipped_nodes）纯函数 seam。

Spec（评论 id 201190348）案 2：零语义变更——A2A 层维持「不干预」，仅把被前向
越过的节点集合标注到事件 payload 并提醒用户。集合计算是唯一有分支的逻辑，
放在无 DB/网络依赖的模块级函数上测试（seam = `compute_skipped_nodes`），
不测 A2A 端点内部实现。

护栏（每条都是 Spec 的验收项）：
- 前向越站 → 中间节点集合（vod→qa = [issue, coding]）；
- 相邻顺推 → 空（不是跳步）；
- 回滚/重派（qa→coding）→ 空（语义与现状一致，不标注、不拒绝）；
- 阶段不可解析 / 目标不在场景 → 空（降级「不标注」，不影响交办）；
- 场景节点显式 `skip` 声明（blog-publish: coding→sre 跳过 qa）→ 空（设计内跳步）；
- payload 空集合不落字段（Spec：缺省不写空数组）。
"""
from __future__ import annotations

import pytest

from src.api.a2a_skip_nodes import (
    attach_skipped_nodes,
    build_skipped_nodes_notice,
    compute_skipped_nodes,
)

DEFAULT_NODES = [
    {"id": "vod", "next": "issue"},
    {"id": "issue", "next": "coding"},
    {"id": "coding", "next": "qa"},
    {"id": "qa", "next": "sre"},
    {"id": "sre", "next": "done"},
]

# blog-publish 场景（configs/pipelines.json）：coding→sre 显式跳过 qa
BLOG_NODES = [
    {"id": "vod", "next": "issue"},
    {"id": "issue", "next": "coding"},
    {"id": "coding", "next": "sre", "skip": ["qa"]},
    {"id": "sre", "next": "done"},
]


def test_forward_skip_collects_intervening_nodes():
    """vod→qa 越过 issue/coding：标注为 skipped_nodes（Spec 验收①的例子）。"""
    assert compute_skipped_nodes(DEFAULT_NODES, "vod", "qa") == ["issue", "coding"]


def test_adjacent_forward_hop_is_not_a_skip():
    """vod→issue 是正常顺推，不算跳步。"""
    assert compute_skipped_nodes(DEFAULT_NODES, "vod", "issue") == []


def test_rollback_hop_is_never_annotated():
    """qa→coding（打回重派）是合法回滚：不标注，语义与现状一致（Spec 验收③）。"""
    assert compute_skipped_nodes(DEFAULT_NODES, "qa", "coding") == []
    assert compute_skipped_nodes(DEFAULT_NODES, "qa", "vod") == []


def test_same_node_reentry_is_not_a_skip():
    """同节点重派（coding→coding）不产生跳步标注。"""
    assert compute_skipped_nodes(DEFAULT_NODES, "coding", "coding") == []


@pytest.mark.parametrize("current", ["", "pending_review", "done", "unknown-node"])
def test_unresolvable_current_stage_degrades_to_empty(current):
    """当前阶段不可解析（空/终态/未知）→ 降级不标注（Spec 风险缓解：不影响交办）。"""
    assert compute_skipped_nodes(DEFAULT_NODES, current, "sre") == []


def test_target_outside_scene_degrades_to_empty():
    """目标不在场景节点链内 → 无路径可算 → 空（不误标）。"""
    assert compute_skipped_nodes(DEFAULT_NODES, "vod", "deploy") == []


def test_declared_scene_skip_is_not_a_skip():
    """场景内显式 `skip` 声明（coding→sre 跳过 qa）是设计内跳步，不标注。"""
    assert compute_skipped_nodes(BLOG_NODES, "coding", "sre") == []


def test_skip_declaration_only_excuses_the_departing_node():
    """`skip` 声明只豁免「声明它的那个节点出发时」的越站：

    coding 声明跳过 qa，只对 coding→sre 生效；从 vod→sre 出发时 qa 仍是未声明
    的越站，照样标注——否则一处声明会把整条链的跳步都掩盖掉。
    """
    nodes = [
        {"id": "vod", "next": "issue"},
        {"id": "issue", "next": "coding"},
        {"id": "coding", "next": "qa", "skip": ["qa"]},
        {"id": "qa", "next": "sre"},
        {"id": "sre", "next": "done"},
    ]
    assert compute_skipped_nodes(nodes, "coding", "sre") == []
    assert compute_skipped_nodes(nodes, "vod", "sre") == ["issue", "coding", "qa"]


def test_attach_skipped_nodes_omits_empty_list():
    """空集合不落字段：Spec 要求「缺省不写空数组」，避免既有 payload 多出噪声键。"""
    payload = {"issue_number": 1, "context": "x"}
    assert attach_skipped_nodes(payload, []) == {"issue_number": 1, "context": "x"}
    assert "skipped_nodes" not in attach_skipped_nodes(payload, [])


def test_attach_skipped_nodes_writes_non_empty_list_and_keeps_input():
    """非空集合写入 payload，且不破坏既有字段、不就地改写入参。"""
    payload = {"issue_number": 1, "context": "x"}
    out = attach_skipped_nodes(payload, ["issue", "coding"])
    assert out["skipped_nodes"] == ["issue", "coding"]
    assert out["issue_number"] == 1 and out["context"] == "x"
    assert "skipped_nodes" not in payload


def test_notice_names_issue_target_and_skipped_nodes():
    """用户提醒必须让用户看清「哪张单、谁跳到谁、中间漏了哪些节点」。"""
    msg = build_skipped_nodes_notice(
        issue_number=2792, target_agent="qa",
        skipped_nodes=["issue", "coding"], scene_id="default")
    assert "#2792" in msg
    assert "qa" in msg
    assert "issue" in msg and "coding" in msg
    assert "default" in msg


def test_notice_for_empty_skip_is_empty():
    """无跳步不发提醒（Spec：仅在 skipped_nodes 非空时发，避免打扰）。"""
    assert build_skipped_nodes_notice(
        issue_number=2792, target_agent="qa", skipped_nodes=[]) == ""