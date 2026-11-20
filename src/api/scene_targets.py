"""Issue #3582: scene node parsing and valid dispatch targets — single source of truth.

``scenes detail`` 节点清单与 ``a2a handoff`` 目标校验此前各自解析场景定义，
口径漂移导致「按 scenes detail 清单发起 handoff 收到 409」。本模块把两处
收敛到同一解析函数（``resolve_scene_targets``）：

- 节点来源统一走 ``get_pipeline_config``（DB 项目场景行 → 市场官方模板 →
  pipelines.json → default，含进程级缓存）；
- 过滤口径统一：``enabled``（节点未启用不在可用清单）+ ``resolvable``
  （``scene_skill_scope.evaluate`` 单一判定器）；
- 目标标识口径统一：节点的 ``id`` 与 ``tag`` 均可作为 handoff 目标
  （与 ``engine_select.node_matches_agent_role`` 的 id 优先/tag 兜底一致），
  ``valid_targets`` 同时收录两者（去重），保证「清单 = handoff 接受集合」；
- 不可派发目标的 ``conflict_reason`` 归因也在此产出（``target_reasons``），
  webhook 仅做投影，不再二次判定。
"""

from __future__ import annotations

from typing import Optional


def attach_node_scope_checks(nodes: list, repo_key: str,
                             scene_id: str = "") -> list:
    """为场景节点附加 resolvable/scope_check（Issue #1721，纯增量）。

    复制每个节点后附加字段，确保不修改 get_pipeline_config 的缓存对象。
    同一响应内按 skill_id/技能名缓存判定结果，避免重复节点触发多次查询。
    """
    result = []
    _cache: dict = {}
    for node in nodes or []:
        if not isinstance(node, dict):
            result.append(node)
            continue
        item = dict(node)
        item.update(node_scope_check(node, repo_key, scene_id, cache=_cache))
        result.append(item)
    return result


def node_scope_check(node: dict, repo_key: str, scene_id: str = "",
                     cache: dict | None = None) -> dict:
    """单节点技能可解析性前置判定（Issue #1721 / #1758）。

    判定统一委托给 ``scene_skill_scope.evaluate``：显式 skill_id 仍需 scope
    校验（user scope 即使由本人显式绑定也不放行），失效 id 回退按名解析，
    查询异常按统一 fail-closed 策略处理。
    返回 {resolvable, scope_check{ok,skill,actual_scope,expected_scope,reason}}。
    """
    from src.api.scene_skill_scope import evaluate
    result = evaluate(node, repo_key, "repo", scene_id=scene_id,
                      cache=cache)
    return {
        "resolvable": bool(result["ok"]),
        "scope_check": {
            "ok": bool(result["ok"]),
            "skill": result.get("skill") or "",
            "actual_scope": result.get("actual_scope") or "",
            "expected_scope": result.get("expected_scope") or "repo",
            "reason": result.get("reason") or "",
        },
    }


def _node_aliases(node: dict) -> list:
    """节点的可接受目标标识：id 与 tag（不同时都收录，保持顺序去重）。

    与 ``engine_select.node_matches_agent_role`` 的「id 优先、tag 兜底」保持
    一致，使 ``valid_targets`` 覆盖 handoff 实际接受的全部标识。
    """
    aliases: list = []
    for key in ("id", "tag"):
        value = node.get(key)
        if value is None or value == "":
            continue
        text = str(value)
        if text not in aliases:
            aliases.append(text)
    return aliases


def resolve_scene_targets(scene_id: str, repo_key: str = "",
                          cfg: Optional[dict] = None) -> dict:
    """同一解析函数：场景节点清单 + 有效派发目标集合（单一数据源）。

    Issue #3582：scenes detail 的节点清单与 a2a handoff 的目标校验均调用
    本函数，保证两处口径一致：

    - ``nodes``：全量节点（复制后附加 resolvable/scope_check）。
      ⚠️ 这是场景定义全量（含 disabled/unresolvable），不是可用派发清单；
      调用方按清单交办时应使用 ``valid_targets`` / ``available_nodes``。
    - ``available_nodes``：可用节点（enabled 且 resolvable），按场景定义顺序；
    - ``valid_targets``：当前有效派发目标清单——可用节点的 id/tag 别名
      （去重，按场景定义顺序），即 handoff 接受的目标集合；
    - ``target_reasons``：不可派发别名 → 冲突原因（node_disabled /
      skill_unresolvable），供 409 ``conflict_reason`` 直接投影；不在场景内
      的目标由调用方回退为 not_in_scene。

    返回 {"scene_id", "cfg", "nodes", "available_nodes", "valid_targets",
    "target_reasons"}；场景解析失败时返回空集合（cfg=None），调用方按既有
    语义处理。
    """
    from src.domains.mgmt.pipeline_config import get_pipeline_config
    # 调用方已持有解析结果（如 scenes detail 的会话绑定二级解析 cfg）时直接
    # 复用，避免按 (scene_id, repo_key) 重查造成口径漂移；未提供时按标准
    # 解析链路读取。
    if cfg is None:
        cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    if not cfg:
        return {"scene_id": scene_id, "cfg": None, "nodes": [],
                "available_nodes": [], "valid_targets": [],
                "target_reasons": {}}
    resolved_id = cfg.get("scene_id") or scene_id
    nodes = attach_node_scope_checks(cfg.get("nodes") or [], repo_key,
                                     scene_id=resolved_id)
    available_nodes: list = []
    valid_targets: list = []
    target_reasons: dict = {}
    for item in nodes:
        if not isinstance(item, dict):
            continue
        aliases = _node_aliases(item)
        if not aliases:
            continue
        enabled = item.get("enabled", True) is not False
        resolvable = bool(item.get("resolvable"))
        if enabled and resolvable:
            available_nodes.append(item)
            for alias in aliases:
                if alias not in valid_targets:
                    valid_targets.append(alias)
            continue
        reason = "node_disabled" if not enabled else "skill_unresolvable"
        for alias in aliases:
            target_reasons[alias] = reason
    return {"scene_id": resolved_id, "cfg": cfg, "nodes": nodes,
            "available_nodes": available_nodes,
            "valid_targets": valid_targets, "target_reasons": target_reasons}
