"""Issue #3582: scene node parsing and valid dispatch targets — single source of truth.

``scenes detail`` 节点清单与 ``a2a handoff`` 目标校验此前各自解析场景定义，
口径漂移导致「按 scenes detail 清单发起 handoff 收到 409」。本模块把两处
收敛到同一解析函数（``resolve_scene_targets``）：

- 节点来源统一走 ``get_pipeline_config``（DB 项目场景行 → 市场官方模板 →
  pipelines.json → default，含进程级缓存）；
- 过滤口径统一：``enabled``（节点未启用不在可用清单）+ ``resolvable``
  （``scene_skill_scope.evaluate`` 单一判定器）；
- 目标标识口径统一（复审 #202444109 + QA 复审回归 #955/#2118）：
  节点的 ``id`` 与 ``tag`` 中**任一**落在平台可派发白名单
  ``DISPATCHABLE_AGENTS`` 的名字均产出目标——组织/存量场景
  （id=org_intake/tag=issue）按 tag 照常产出 ``issue``，标准场景
  （id=coding/tag=中文阶段名）按 id 产出 ``coding``；
  ``valid_targets`` 只含实际可投递角色，与 POST /api/a2a 的接受集合
  一致（展示用 tag 阶段名不在白名单时不可作为 handoff 目标，否则
  清单会宣告被 400 拒绝的名字）；
- 不可派发目标的 ``conflict_reason`` 归因也在此产出（``target_reasons``），
  webhook 仅做投影，不再二次判定；原因字面量以本模块常量为单一来源。

已知限制（Issue #3582 复审 AC4，QA 裁定接受为已知限制）：写路径
（set_scene_nodes / scene_nodes_write_by_id）已在**本进程**立即失效
``get_pipeline_config`` 缓存；但 webhook（event 进程）与 scenes detail
（mgmt/api 进程）各持进程级缓存，**跨进程**场景定义变更后的最长陈旧窗口
仍为缓存 TTL 60s（无跨进程失效通道）——同一进程内两处读取口径恒一致，
跨进程在 TTL 窗口后自动收敛。
"""

from __future__ import annotations

from typing import Optional

# ===== Issue #3582 复审（PR 评论 #202444109）：冲突原因常量（单一来源） =====
# resolver 归因 / webhook 409 投影共用，避免字面量散落导致两侧口径漂移。
REASON_NOT_IN_SCENE = "not_in_scene"
REASON_NODE_DISABLED = "node_disabled"
REASON_SKILL_UNRESOLVABLE = "skill_unresolvable"


def _attach_node_scope_checks(nodes: list, repo_key: str,
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
        item.update(_node_scope_check(node, repo_key, scene_id, cache=_cache))
        result.append(item)
    return result


def _node_scope_check(node: dict, repo_key: str, scene_id: str = "",
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


def dispatchable_roles(node) -> list:
    """节点落在可派发白名单内的全部目标名（id 或 tag 均产出）。

    Issue #3582 QA 复审（回归阻塞项 #955/#2118）：此前的 ``_dispatchable_role``
    复用 ``engine_select.node_agent_role``（id 优先、tag 仅 id 为空时兜底），
    导致组织/存量场景 ``id=org_intake/tag=issue`` 这类节点被整体丢弃——
    ``valid_targets=[]``，POST /api/a2a 交办 issue/coding/qa 全部 409。

    修复口径（与基线 ``node_matches_agent_role`` 的 id OR tag 放行对齐）：
    - 依次检查 ``id``、``tag``，凡落在 ``DISPATCHABLE_AGENTS`` 白名单内的
      名字均产出（去重、保持出现顺序）；
    - 展示用 tag（中文阶段名）与白名单外的 id（org_*）自然不产出——它们
      会被 POST /api/a2a 的白名单校验 400 拒绝，不得进清单；
    - 标准场景 id=coding/tag=中文 与存量 tag-only 节点口径不变。
    """
    from src.api.pipeline_constants import DISPATCHABLE_AGENTS
    if not isinstance(node, dict):
        return []
    roles: list = []
    for name in (node.get("id"), node.get("tag")):
        role = str(name or "").strip()
        if role and role in DISPATCHABLE_AGENTS and role not in roles:
            roles.append(role)
    return roles


def node_dispatch_names(node) -> list:
    """handoff 可按哪些名字命中该节点（单一谓词，供 webhook 投影）。

    Issue #3582 复审：webhook 侧 ``id == target or target in dispatchable_roles``
    的重复判定收敛到本模块——白名单角色（id 或 tag 命中）之外，节点自身的
    id 也算命中（#2118 组织/存量场景的 ``org_*`` id 直达别名），命中语义
    与 POST /api/a2a 的白名单 400 前置校验叠加后即实际接受集合。
    """
    names = dispatchable_roles(node)
    if isinstance(node, dict):
        nid = str(node.get("id") or "").strip()
        if nid and nid not in names:
            names.append(nid)
    return names


def empty_scene_targets(scene_id: str) -> dict:
    """场景解析失败/解析异常时的空结果（与正常结果键结构同构，单一来源）。

    供 ``resolve_scene_targets`` 空分支和调用方异常兜底共用，避免键结构在
    多处复制后漂移（webhook except 分支曾重复字面量，Issue #3582 复审）。
    """
    return {"scene_id": scene_id, "cfg": None, "nodes": [],
            "available_nodes": [], "valid_targets": [],
            "target_reasons": {}}


def resolve_scene_targets(scene_id: str, repo_key: str = "",
                          cfg: Optional[dict] = None) -> dict:
    """同一解析函数：场景节点清单 + 有效派发目标集合（单一数据源）。

    Issue #3582：scenes detail 的节点清单与 a2a handoff 的目标校验均调用
    本函数，保证两处口径一致：

    - ``nodes``：全量节点（复制后附加 resolvable/scope_check）。
      ⚠️ 这是场景定义全量（含 disabled/unresolvable），不是可用派发清单；
      调用方按清单交办时应使用 ``valid_targets`` / ``available_nodes``。
    - ``available_nodes``：可用节点（enabled 且 resolvable），按场景定义顺序；
    - ``valid_targets``：当前有效派发目标清单——可用节点的可投递角色名
      （id 或 tag 中落在 ``DISPATCHABLE_AGENTS`` 白名单内的名字，去重，
      按场景定义顺序），即 POST /api/a2a 实际接受的目标集合；
    - ``target_reasons``：不可派发角色 → 冲突原因（node_disabled /
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
        return empty_scene_targets(scene_id)
    resolved_id = cfg.get("scene_id") or scene_id
    nodes = _attach_node_scope_checks(cfg.get("nodes") or [], repo_key,
                                     scene_id=resolved_id)
    available_nodes: list = []
    valid_targets: list = []
    target_reasons: dict = {}
    for item in nodes:
        if not isinstance(item, dict):
            continue
        roles = dispatchable_roles(item)
        if not roles:
            # 展示用 tag（非白名单角色）与白名单外 id（org_*）不是可投递
            # 目标：既不进 valid_targets（POST /api/a2a 会按白名单 400
            # 拒绝），也不进 target_reasons（handoff 到达 _scene_node_check
            # 的目标必先通过 400 白名单）。
            continue
        enabled = item.get("enabled", True) is not False
        resolvable = bool(item.get("resolvable"))
        if enabled and resolvable:
            available_nodes.append(item)
            for role in roles:
                if role not in valid_targets:
                    valid_targets.append(role)
            continue
        reason = REASON_NODE_DISABLED if not enabled else REASON_SKILL_UNRESOLVABLE
        for role in roles:
            target_reasons[role] = reason
    return {"scene_id": resolved_id, "cfg": cfg, "nodes": nodes,
            "available_nodes": available_nodes,
            "valid_targets": valid_targets, "target_reasons": target_reasons}
