"""Issue #3582: scene node parsing and valid dispatch targets — single source of truth.

``scenes detail`` 节点清单与 ``a2a handoff`` 目标校验此前各自解析场景定义，
口径漂移导致「按 scenes detail 清单发起 handoff 收到 409」。本模块把两处
收敛到同一解析函数（``resolve_scene_targets``）：

- 节点来源统一走 ``get_pipeline_config``（DB 项目场景行 → 市场官方模板 →
  pipelines.json → default，含进程级缓存）；
- 过滤口径统一：``enabled``（节点未启用不在可用清单）+ ``resolvable``
  （``scene_skill_scope.evaluate`` 单一判定器）；
- 调用方（scenes detail / a2a handoff）只做结果投影，不再各自组装清单，
  场景定义变更后两处同步生效（同一解析 + 同一缓存失效链路）。
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


def resolve_scene_targets(scene_id: str, repo_key: str = "",
                          cfg: Optional[dict] = None) -> dict:
    """同一解析函数：场景节点清单 + 有效派发目标集合（单一数据源）。

    Issue #3582：scenes detail 的可用节点清单与 a2a handoff 的目标校验
    均调用本函数，保证两处口径一致：

    - ``nodes``：全量节点（复制后附加 resolvable/scope_check，展示用）；
    - ``valid_targets``：当前有效目标清单（enabled 且 resolvable 的节点
      id 列表，按场景定义顺序）——scenes detail 作为可用清单回显，
      a2a handoff 作为目标校验与 409 冲突详情的依据；
    - ``node_status``：{node_id: {enabled, resolvable, reason}}，供 409
      conflict_reason 归因（node_disabled / skill_unresolvable）。

    返回 {"scene_id", "cfg", "nodes", "valid_targets", "node_status"}；
    场景解析失败时返回空集合（cfg=None），调用方按既有语义处理。
    """
    from src.domains.mgmt.pipeline_config import get_pipeline_config
    # 调用方已持有解析结果（如 scenes detail 的会话绑定二级解析 cfg）时直接
    # 复用，避免按 (scene_id, repo_key) 重查造成口径漂移；未提供时按标准
    # 解析链路读取。
    if cfg is None:
        cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    if not cfg:
        return {"scene_id": scene_id, "cfg": None, "nodes": [],
                "valid_targets": [], "node_status": {}}
    resolved_id = cfg.get("scene_id") or scene_id
    nodes = attach_node_scope_checks(cfg.get("nodes") or [], repo_key,
                                     scene_id=resolved_id)
    valid_targets: list = []
    node_status: dict = {}
    for item in nodes:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or item.get("tag") or "")
        enabled = item.get("enabled", True) is not False
        resolvable = bool(item.get("resolvable"))
        if enabled and resolvable:
            valid_targets.append(node_id)
        node_status[node_id] = {
            "enabled": enabled,
            "resolvable": resolvable,
            "reason": (item.get("scope_check") or {}).get("reason") or "",
        }
    return {"scene_id": resolved_id, "cfg": cfg, "nodes": nodes,
            "valid_targets": valid_targets, "node_status": node_status}
