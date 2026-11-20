"""Issue #2792（#2747 子单4）A2A 前向跳步披露：skipped_nodes 计算与用户提醒文案。

背景：A2A 层按平台显式设计「不做跳步修正——agent 有权 A2A 到任意目标 agent
（含回滚）」（见 `routes/webhook.py::_advance_a2a_issue_stage`）。结果是一个
前向越站交办（如 vod 自行完成后直交 qa，跳过 issue/coding）在事件链上看不出
中间节点「被跳过」——Issue 页面只显示已执行的那一段，像是节点凭空消失。

Spec（Issue 评论 id 201190348）案 2：**零语义变更**——A2A 层维持不干预，只把
被越过的节点集合标注到事件 payload（`skipped_nodes`）并向用户提醒。案 1
（前向越站 422 拒绝）需平台决策，本模块不实现。

本模块是纯函数层（无 DB / 无网络 / 无日志），让「哪些节点算被跳过」这条唯一
有分支的规则可以被单独测试，端点只做连线：

    skipped = compute_skipped_nodes(nodes, current_stage, target_agent)
    payload = attach_skipped_nodes(payload, skipped)      # 空集合不落键
    notice  = build_skipped_nodes_notice(...)             # 空集合返回空串

判定口径（每条都是 Spec 的护栏）：
- 只有「目标位于当前节点之后且不相邻」才算跳步；
- 目标在前（qa→coding 打回、qa→vod 重派）属合法回滚，恒返回空；
- 当前阶段无法解析（空 / pending_review / done / 不在场景内）→ 降级为空
  （Spec 风险缓解：无法解析时按「不标注」降级，不影响交办）；
- 场景节点显式 `skip` 声明（configs/pipelines.json 中 blog-publish 的
  coding→sre 跳过 qa）是场景设计内跳步，不算异常，不标注。
"""
from __future__ import annotations

__all__ = [
    "REJECT_FORWARD_SKIP",
    "compute_skipped_nodes",
    "attach_skipped_nodes",
    "build_skipped_nodes_notice",
]

SKIPPED_NODES_FIELD = "skipped_nodes"

#: Issue #2792 案 1 开关位（A2A 前向越站拒绝）——**默认关闭**，本期零语义变更。
#:
#: Spec 明确案 1「语义收窄需平台决策，本 Spec 不实现；实现时预留开关位」。开关放在
#: 这里而不是端点里，是为了让「要不要拒绝」成为一处可指认的平台决策，而跳步集合的
#: 计算口径（`compute_skipped_nodes`）在两种案下完全复用：采纳案 1 只需把本常量改为
#: True（端点据此把非空 `skipped_nodes` 转成 422 拒绝），不必再算一遍差集。
#: 回滚方向（`qa→coding` 等）在两案下都不受限制。
REJECT_FORWARD_SKIP = False


def _node_ids(nodes) -> list:
    """场景节点 id 序列（保持 pipelines.json 的声明顺序 = 流水线正序）。"""
    ids = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        _id = str(node.get("id") or "").strip()
        if _id:
            ids.append(_id)
    return ids


def _declared_skips(nodes, node_id: str) -> set:
    """当前节点显式声明的 `skip` 列表（场景设计内跳步，不算异常跳步）。"""
    for node in nodes or []:
        if isinstance(node, dict) and str(node.get("id") or "").strip() == node_id:
            declared = node.get("skip") or []
            if isinstance(declared, str):
                declared = [declared]
            return {str(x).strip() for x in declared if str(x).strip()}
    return set()


def compute_skipped_nodes(nodes, current_stage: str, target_agent: str) -> list:
    """计算前向跳步集合：当前节点与目标节点之间被越过的节点 id（正序）。

    - `nodes`：场景节点列表（`[{"id": ..., "next": ..., "skip": [...]}]`），
      来自 `_scene_node_check` 已读取的同一份场景配置；
    - `current_stage`：Issue 当前归属阶段（`get_issue_stage` 的 stage）；
    - `target_agent`：本次 A2A 的目标 agent。

    返回被跳过的节点 id 列表；非前向越站 / 不可解析 / 全部为声明内跳步时返回 []。
    """
    ids = _node_ids(nodes)
    current = str(current_stage or "").strip()
    target = str(target_agent or "").strip()
    if not ids or not current or not target or current == target:
        return []
    if current not in ids or target not in ids:
        return []
    current_idx, target_idx = ids.index(current), ids.index(target)
    if target_idx <= current_idx:
        # 回滚 / 重派 / 同节点：不是前向跳步，语义与现状一致（不标注）
        return []
    declared = _declared_skips(nodes, current)
    return [nid for nid in ids[current_idx + 1:target_idx] if nid not in declared]


def attach_skipped_nodes(payload: dict, skipped_nodes) -> dict:
    """把跳步集合并入事件 payload；空集合不落键（Spec：缺省不写空数组）。

    返回新 dict，不改写入参：payload 在 a2a 端点内会被多处复用（幂等指纹、
    排队入队、事件落库），就地改写会让先算出来的指纹与落库内容不一致。
    """
    out = dict(payload or {})
    cleaned = [str(n).strip() for n in (skipped_nodes or []) if str(n).strip()]
    if cleaned:
        out[SKIPPED_NODES_FIELD] = cleaned
    return out


def build_skipped_nodes_notice(issue_number, target_agent: str,
                               skipped_nodes, scene_id: str = "") -> str:
    """生成 Issue 侧用户提醒文案；无跳步时返回空串（调用方据此决定不发评论）。

    提醒必须让用户一眼看清「哪张单、跳到了谁、中间漏了哪些节点、现在的后果」，
    否则标注只对读 payload 的人有意义。
    """
    cleaned = [str(n).strip() for n in (skipped_nodes or []) if str(n).strip()]
    if not cleaned:
        return ""
    target = str(target_agent or "").strip()
    scene_part = f"（场景 `{scene_id}`）" if str(scene_id or "").strip() else ""
    nodes_part = " → ".join(cleaned)
    return (
        f"⚠️ A2A 前向跳步提醒{scene_part}：本次交办直接派给 `{target}`，"
        f"跳过了中间节点 `{nodes_part}`。\n\n"
        f"- 被跳过的节点本轮未执行，其结论/产物不会出现在事件链上；\n"
        f"- 跳步已被如实标注在事件 payload 的 `skipped_nodes` 上，"
        f"回滚/重派路径不受影响；\n"
        f"- 如需补齐，请对 `{target}` 之前被跳过的节点单独发起 A2A，"
        f"或待平台决策后启用前向越站拒绝（Spec 案 1）。\n\n"
        f"_Issue #{issue_number} · auto reported by grape A2A_\n"
    )