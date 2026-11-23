# -*- coding: utf-8 -*-
"""组织下派终态 → org_aggregate 重排事件（runtime 域实现）。

从 ``src/api/a2a_dispatch.py`` 下沉：组织聚合事件属于 runtime 事件/队列语义，
放在 runtime 域后，mgmt 侧只能经 ``src.domains.runtime.public`` 契约调用（H3），
不再跨域 import API 装配模块。

幂等键 ``org_agg:<dispatch_id>:<terminal_status>`` 由 events 唯一索引兜底；
事件以 queued 持久化，由巡检 ``reconcile_queued_a2a`` 按 ``_not_before`` 补派。
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.kernel.config import log
from src.kernel.issue_ident import is_actionable_issue_number, native_issue_number
from src.kernel.repo_key import from_repo_key


def trigger_org_aggregate(dispatch: dict, targets: list[dict] | None,
                          terminal_status: str) -> dict:
    """批次进入终态时幂等持久化一条 org_aggregate 重排事件。

    返回 ``{"status": "queued"|"dedup"|"skipped", "seq": N}``。
    """
    from src.domains.mgmt.public import (
        get_pipeline_config,
        get_pipeline_transition,
        node_role,
    )
    from src.domains.runtime.event_store import append_event, find_event_by_idempotency_key

    repo_key = str((dispatch or {}).get("org_repo_key") or "")
    issue_number = native_issue_number((dispatch or {}).get("org_issue_number") or "")
    dispatch_id = str((dispatch or {}).get("id") or "")
    scene_id = str((dispatch or {}).get("scene_id") or "") or "default"
    if not repo_key or not is_actionable_issue_number(issue_number) or not dispatch_id:
        return {"status": "skipped", "seq": 0}

    idem = f"org_agg:{dispatch_id}:{str(terminal_status or '')}"[:128]
    existing = find_event_by_idempotency_key(repo_key, idem)
    if existing:
        return {"status": "dedup", "seq": int(existing.get("seq") or 0)}

    transition = get_pipeline_transition(scene_id, "org_aggregate", repo_key=repo_key) or {}
    node = transition.get("node") or {}
    if not node:
        # 自定义组织场景可能给聚合节点换了 id/tag，但技能语义仍是 org-aggregate-cli。
        # 按技能兜底解析，避免聚合事件没有目标技能而永远派发不出去。
        cfg = get_pipeline_config(scene_id, repo_key=repo_key) or {}
        node = next(
            (n for n in (cfg.get("nodes") or [])
             if str(n.get("skill") or "") == "org-aggregate-cli"),
            None,
        ) or {}
    role = node_role(node) or "qa"
    repository = from_repo_key(repo_key) or repo_key
    context = (
        f"组织下派 #{dispatch_id} 已进入终态 {terminal_status}，请聚合各目标结果并收口组织 Issue"
    )
    payload = {
        "repository": repository,
        "account": repository,
        "issue_number": str(issue_number),
        "pr_number": "",
        "context": context[:500],
        "aggregate_recheck": True,
        "aggregate_round": 1,
        "org_id": str((dispatch or {}).get("org_id") or ""),
        "dispatch_id": dispatch_id,
        "terminal_status": str(terminal_status or ""),
        "ref_seq": 0,
        # 到点即可由巡检派发（sweeper 已确认批次终态）
        "_not_before": datetime.now(timezone.utc).isoformat(),
    }
    evt = {
        "type": "a2a_event", "repository": repository, "account": repository,
        "issue_id": f"#{issue_number}", "issue_number": str(issue_number),
        "payload": {k: v for k, v in payload.items() if k != "_a2a_queue"},
        "_a2a_context": context,
    }
    payload["_a2a_queue"] = {
        "existing_seq": 0,
        "dispatch": {
            "skill_name": str(node.get("skill") or ""),
            "skill_id": int(node.get("skill_id") or 0),
            "skill_path": str(node.get("skill_path") or ""),
            "runtime_id": 0,
            "backend": "",
            "evt": evt,
        },
    }
    try:
        seq = append_event(
            repo_key=repo_key, event_type="a2a_event",
            title=f"组织聚合重排：dispatch #{dispatch_id} 终态 {terminal_status}",
            issue_id=f"#{issue_number}", source="a2a", agent_type=role,
            summary=f"A2A: → {role} (org aggregate recheck)",
            payload=payload, status="queued", scene_id=scene_id,
            idempotency_key=idem,
        )
    except Exception as exc:  # noqa: BLE001 - 持久化失败不阻断批次 sweeper
        log.warning(f"org-aggregate: persist recheck event failed dispatch={dispatch_id}: {exc}")
        return {"status": "skipped", "seq": 0}
    # 批次终态时才触发聚合节点，这里同步把组织 Issue stage 推进到聚合角色。
    try:
        from src.api.session_store import set_issue_stage
        set_issue_stage(repo_key, f"#{issue_number}", role, "org_aggregate")
    except Exception as exc:  # noqa: BLE001 - stage 写失败由对账器兜底
        log.warning(f"org-aggregate: set issue stage failed issue=#{issue_number}: {exc}")
    if seq <= 0:
        # 撞幂等键：已有事件（负 seq），不再触发
        return {"status": "dedup", "seq": abs(int(seq))}
    log.info(f"org-aggregate: queued recheck event seq={seq} dispatch={dispatch_id} "
             f"status={terminal_status} role={role} scene={scene_id}")
    return {"status": "queued", "seq": int(seq)}