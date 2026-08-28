"""Pipeline Executor (event-dispatch-redesign-v2.md Phase 4).

Executes pipeline nodes using the 4 DSH event modes.
Bridges the event bus to Grape's existing enqueue/dispatch infrastructure.

Each node dispatches via the event bus:
- emit (default): task enqueued to tag queue, first engine claims
- waterfall: middleware chain, can intercept before dispatch
- serial: multiple agents process sequentially
- parallel: multiple agents run concurrently, wait for all
"""
import asyncio
import logging
from typing import Any

from src.api.event_bus import get_event_bus, GrapeEvent, EventDomain
from src.api.pipeline_config import get_pipeline_config, get_node_by_tag, get_next_node, is_node_skipped
from src.config import log

_bus = get_event_bus()


async def dispatch_node(
    scene_id: str,
    node_id: str,
    repo_key: str,
    issue_id: str,
    event_seq: int,
    evt: dict,
    skill_name: str = "",
    skill_id: int = 0,
    skill_path: str = "",
    runtime_id: int = 0,
    pool_tag: str = "",
) -> dict:
    """Dispatch a pipeline node using its configured event mode.

    Returns {"status": "dispatched"|"blocked"|"failed", "node": node_id, "mode": mode}
    """
    cfg = get_pipeline_config(scene_id)
    if not cfg:
        return {"status": "failed", "node": node_id, "mode": "none", "error": "no pipeline config"}

    node = None
    for n in cfg.get("nodes", []):
        if n["id"] == node_id:
            node = n
            break
    if not node:
        return {"status": "failed", "node": node_id, "mode": "none", "error": "node not found"}

    mode = node.get("mode", "emit")
    tag = node.get("tag", node_id)

    # Emit pipeline/node/dispatched event (fire-and-forget)
    await _bus.emit(GrapeEvent(
        domain=EventDomain.PIPELINE,
        type="pipeline/node/dispatched",
        repo_key=repo_key,
        issue_id=issue_id,
        agent_type=tag,
        seq=event_seq,
        payload={"node_id": node_id, "mode": mode, "skill": node.get("skill", "")},
    ))

    if mode == "emit":
        return await _dispatch_emit(node, repo_key, issue_id, event_seq, evt,
                                    skill_name, skill_id, skill_path, runtime_id, pool_tag)

    elif mode == "waterfall":
        return await _dispatch_waterfall(node, repo_key, issue_id, event_seq, evt,
                                         skill_name, skill_id, skill_path, runtime_id, pool_tag)

    elif mode == "serial":
        return await _dispatch_serial(node, repo_key, issue_id, event_seq, evt,
                                      skill_name, skill_id, skill_path, runtime_id, pool_tag)

    elif mode == "parallel":
        return await _dispatch_parallel(node, repo_key, issue_id, event_seq, evt,
                                        skill_name, skill_id, skill_path, runtime_id, pool_tag)

    else:
        log.warning(f"Pipeline executor: unknown mode '{mode}' for node {node_id}, falling back to emit")
        return await _dispatch_emit(node, repo_key, issue_id, event_seq, evt,
                                    skill_name, skill_id, skill_path, runtime_id, pool_tag)


async def _dispatch_emit(node, repo_key, issue_id, event_seq, evt,
                         skill_name, skill_id, skill_path, runtime_id, pool_tag):
    """emit mode: enqueue task to tag queue, first engine claims."""
    tag = node.get("tag", node["id"])
    _pool_tag = pool_tag or tag

    # Enqueue via existing infrastructure (sync call wrapped in executor)
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        _enqueue_via_pipeline,
        repo_key, event_seq, node.get("tag", node["id"]),
        node.get("skill", skill_name), evt, runtime_id, issue_id, _pool_tag,
        skill_id, skill_path,
    )
    if result.get("error"):
        await _bus.emit(GrapeEvent(
            domain=EventDomain.PIPELINE, type="pipeline/node/failed",
            repo_key=repo_key, issue_id=issue_id, agent_type=tag, seq=event_seq,
            payload={"node_id": node["id"], "error": result["error"]},
        ))
        return {"status": "failed", "node": node["id"], "mode": "emit", "error": result["error"]}

    await _bus.emit(GrapeEvent(
        domain=EventDomain.PIPELINE, type="pipeline/node/queued",
        repo_key=repo_key, issue_id=issue_id, agent_type=tag, seq=event_seq,
        payload={"node_id": node["id"], "pool_tag": _pool_tag, "task_id": result.get("task_id")},
    ))
    return {"status": "dispatched", "node": node["id"], "mode": "emit", "task_id": result.get("task_id")}


async def _dispatch_waterfall(node, repo_key, issue_id, event_seq, evt,
                              skill_name, skill_id, skill_path, runtime_id, pool_tag):
    """waterfall mode: middleware chain, listeners can intercept before dispatch.

    Listeners on "pipeline/node/pre-dispatch" receive (event, value, next_fn).
    If a listener returns {"block": True}, dispatch is skipped.
    """
    tag = node.get("tag", node["id"])
    event = GrapeEvent(
        domain=EventDomain.PIPELINE,
        type="pipeline/node/pre-dispatch",
        repo_key=repo_key, issue_id=issue_id, agent_type=tag, seq=event_seq,
        payload={"node_id": node["id"], "skill": node.get("skill", "")},
    )
    decision = await _bus.waterfall(event, {"block": False})

    if decision and decision.get("block"):
        log.info(f"Pipeline waterfall: node {node['id']} blocked by listener")
        await _bus.emit(GrapeEvent(
            domain=EventDomain.PIPELINE, type="pipeline/node/blocked",
            repo_key=repo_key, issue_id=issue_id, agent_type=tag, seq=event_seq,
            payload={"node_id": node["id"], "reason": decision.get("reason", "blocked")},
        ))
        return {"status": "blocked", "node": node["id"], "mode": "waterfall",
                "reason": decision.get("reason", "blocked")}

    # Not blocked → proceed with normal emit dispatch
    return await _dispatch_emit(node, repo_key, issue_id, event_seq, evt,
                                skill_name, skill_id, skill_path, runtime_id, pool_tag)


async def _dispatch_serial(node, repo_key, issue_id, event_seq, evt,
                           skill_name, skill_id, skill_path, runtime_id, pool_tag):
    """serial mode: multiple agents process sequentially.

    Uses "pipeline/node/serial-step" listeners. Each listener processes the event
    in order; first non-None result wins.
    """
    tag = node.get("tag", node["id"])
    listeners = node.get("listeners") or [node]

    for listener_config in listeners:
        l_tag = listener_config.get("tag", tag)
        l_skill = listener_config.get("skill", skill_name)
        _pool_tag = pool_tag or l_tag

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            _enqueue_via_pipeline,
            repo_key, event_seq, l_tag, l_skill, evt, runtime_id, issue_id, _pool_tag,
            skill_id, skill_path,
        )
        if result.get("error"):
            log.warning(f"Pipeline serial step failed: {result['error']}")
            return {"status": "failed", "node": node["id"], "mode": "serial",
                    "error": result["error"], "failed_listener": l_tag}

    return {"status": "dispatched", "node": node["id"], "mode": "serial"}


async def _dispatch_parallel(node, repo_key, issue_id, event_seq, evt,
                             skill_name, skill_id, skill_path, runtime_id, pool_tag):
    """parallel mode: multiple agents run concurrently, wait for all.

    All listeners are dispatched simultaneously; results gathered.
    """
    tag = node.get("tag", node["id"])
    listeners = node.get("listeners") or [node]

    async def _dispatch_one(listener_config):
        l_tag = listener_config.get("tag", tag)
        l_skill = listener_config.get("skill", skill_name)
        _pool_tag = pool_tag or l_tag
        return await asyncio.get_event_loop().run_in_executor(
            None,
            _enqueue_via_pipeline,
            repo_key, event_seq, l_tag, l_skill, evt, runtime_id, issue_id, _pool_tag,
            skill_id, skill_path,
        )

    results = await asyncio.gather(*[_dispatch_one(lc) for lc in listeners], return_exceptions=True)

    errors = [str(r) for r in results if isinstance(r, Exception) or (isinstance(r, dict) and r.get("error"))]
    if errors:
        return {"status": "failed", "node": node["id"], "mode": "parallel", "errors": errors}

    return {"status": "dispatched", "node": node["id"], "mode": "parallel"}


def _enqueue_via_pipeline(repo_key, seq, agent_type, skill_name, evt,
                          runtime_id, issue_id, pool_tag, skill_id=0, skill_path=""):
    """Sync wrapper: calls _enqueue_pipeline_task from event_pipeline.py."""
    try:
        from src.api.event_pipeline import _enqueue_pipeline_task, is_plugin_mode
        from src.runtime.engine_select import pick_engine
        from src.session_manager import db, db_lock

        # Validate runtime_id: if the engine no longer exists or is offline,
        # drop it to 0 so the task becomes a pool task (claim_by_tag can pick it up).
        # This prevents stale devops_agent_session.runtime_id from pinning tasks
        # to non-existent engines.
        if runtime_id:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT 1 FROM agent_runtimes WHERE id=? AND status='online'", (runtime_id,))
                if not c.fetchone():
                    log.info(f"Pipeline emit: runtime_id={runtime_id} not online, dropping to pool task (agent={agent_type})")
                    runtime_id = 0

        # Plugin 模式：始终走 pick_engine（返回 0=广播），忽略上游传入的 runtime_id。
        # 上游 runtime_id 来自发起 A2A 的 agent 自己运行的引擎（如 QA 跑在 0003 的 rt=54 上，
        # 传 54 给 coding），直接用它会绕过 tag 限制把任务定向到 0003。
        # 非 plugin 模式：保留原逻辑（有 runtime_id 直接用，无则 pick_engine）。
        if is_plugin_mode():
            _picked = pick_engine(repo_key, agent_type, fallback_runtime_id=0)
            if _picked is None:
                return {"error": "no engine available"}
            runtime_id = _picked
        elif not runtime_id:
            _picked = pick_engine(repo_key, agent_type, fallback_runtime_id=0)
            if _picked is None:
                return {"error": "no engine available"}
            runtime_id = _picked

        _enqueue_pipeline_task(
            repo_key, seq, agent_type, "", skill_name, evt,
            skill_id, skill_path, runtime_id, issue_id,
        )
        return {"task_id": "enqueued"}
    except Exception as e:
        return {"error": str(e)[:200]}
