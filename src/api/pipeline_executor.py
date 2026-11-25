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

from src.kernel.config import log

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
    user_id: str = "",
) -> dict:
    """Dispatch a pipeline node using its configured event mode.

    Returns {"status": "dispatched"|"blocked"|"failed", "node": node_id, "mode": mode}
    """
    cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    if not cfg:
        return {"status": "failed", "node": node_id, "mode": "none", "error": "no pipeline config"}

    node = None
    from src.domains.runtime.engine.engine_select import node_matches_agent_role
    for n in cfg.get("nodes", []):
        if not isinstance(n, dict):
            continue
        if n.get("id") == node_id or node_matches_agent_role(n, node_id):
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
        from src.domains.runtime.pipeline.event_pipeline import _enqueue_pipeline_task, is_plugin_mode
        from src.domains.runtime.engine.engine_select import (
            POOL_SOURCE_DIRECTED, directed_pool_engine_ids, pick_engine,
            resolve_directive_engine, resolve_scene_user_scope,
        )

        # Issue #2791（P1-8）：A2A 交办携带 `engine_config.runtime_id` 时，定向派发优先于池选择。
        # 命中 → 锁定该引擎并跳过 pick_engine / 场景池解析（场景池全灭也不否决定向派发）；
        # 未命中 → 落到下面的既有池选择路径，结构化 WARN 由 _enqueue_pipeline_task 统一输出
        #（那层能拿到最终 pool，WARN 才写得清「最终选中引擎」）。
        try:
            _directive = resolve_directive_engine(
                (evt.get("payload") or {}).get("engine_config"), repo_key, agent_type)
        except Exception as _dir_e:
            log.warning(f"Pipeline emit: resolve engine_config directive failed: {_dir_e}")
            _directive = None
        if _directive and _directive.get("hit"):
            _dir_rid = _directive["runtime_id"]
            log.info(f"Pipeline emit (directed): engine_config.runtime_id={_dir_rid} "
                     f"({_directive.get('engine_name') or '-'}) locked, pool selection skipped, seq={seq}")
            # 场景仍要解析出 user scope（技能 scope 门禁/任务行 user_id 依赖它），
            # 但**不读场景池**——定向派发就是跳过池选择。解析单点 resolve_scene_user_scope。
            _dir_uid = resolve_scene_user_scope(
                repo_key, issue_id, evt.get("actor", ""))[1]
            _enqueue_pipeline_task(
                repo_key, seq, agent_type, "", skill_name, evt,
                skill_id, skill_path, _dir_rid, issue_id,
                pool_engine_ids=directed_pool_engine_ids(_dir_rid), user_id=_dir_uid,
                pool_engine_source=POOL_SOURCE_DIRECTED,
            )
            return {"task_id": "enqueued"}

        # Validate runtime_id: if the engine no longer exists or is offline,
        # drop it to 0 so the task becomes a pool task (claim_by_tag can pick it up).
        # This prevents stale devops_agent_session.runtime_id from pinning tasks
        # to non-existent engines.
        if runtime_id:
            # Issue #259：status=online 且心跳新鲜（< _RUNTIME_STALE_SECONDS，默认 100s）才算在线。
            # 原 status-only 检查会被「刚断线、reap 还没翻牌」的窗口期欺骗。
            # 判定（含阈值与时间戳解析）归 runtime 域：agent_runtimes 是它的表，
            # api 侧只拿布尔，不再在自己连接上直读该表（独立拓扑下那条 SQL 必 1146）。
            from src.domains.runtime import public as _runtime_public
            if not _runtime_public.runtime_heartbeat_alive_any(runtime_id):
                log.info(f"Pipeline emit: runtime_id={runtime_id} offline or stale heartbeat, dropping to pool task (agent={agent_type})")
                runtime_id = 0

        # Plugin 模式：始终走 pick_engine（返回 0=广播），忽略上游传入的 runtime_id。
        # 上游 runtime_id 来自发起 A2A 的 agent 自己运行的引擎（如 QA 跑在 0003 的 rt=54 上，
        # 传 54 给 coding），直接用它会绕过 tag 限制把任务定向到 0003。
        # 非 plugin 模式：保留原逻辑（有 runtime_id 直接用，无则 pick_engine）。
        _pool_eids = ""
        _pool_sel_a2a = ""
        _pool_src_a2a = ""
        _scene_uid = 0
        if is_plugin_mode():
            # 获取 issue 级场景 + 场景 user scope（解析/异常留痕/反查兜底单点）
            _scene_id, _scene_uid = resolve_scene_user_scope(
                repo_key, issue_id, evt.get("actor", ""))
            _picked = pick_engine(repo_key, agent_type, fallback_runtime_id=0,
                                  scene_id=_scene_id, user_id=_scene_uid,
                                  issue_creator=evt.get("actor", ""))
            if _picked is None:
                return {"error": "no engine available"}
            runtime_id = _picked
            # [#1208 A2A selector 修复 2026-11-12] 事件驱动 A2A 入队补三分叉 gate——
            # 原路径只读 id 池（selector 场景恒空），selector 节点的任务退化为广播抢单
            # （实测 A2A 直发 → 任意引擎（含圈外个人级引擎）接单后被凭据门禁拦下）。
            # 与 run_pipeline 的 gate 同口径：selector 模式落 pool_selector、两池列空；
            # 零命中 fail-closed（上层 listener 已有 _fail_event 错误管道）。
            # Issue #3790 审查 R1：门禁是池语义的唯一解析口，池名单与来源（scene_missing）
            # 一并取回并显式透传入队——原「先单独取池 + 再 gate」把来源丢弃，scene_missing
            # 行退化为普通场景绑定，且重复触发一次告警计数。
            _gate_resolved = False
            try:
                from src.domains.runtime.pipeline.event_pipeline import _scene_pool_gate
                _gm_a2a, _gp_a2a, _gf_a2a = _scene_pool_gate(
                    _scene_id, agent_type, repo_key=repo_key, user_id=_scene_uid,
                    issue_creator=evt.get("actor", ""))
                if _gm_a2a == "selector":
                    _pool_sel_a2a = _gp_a2a["pool_selector"]
                    _pool_eids = ""   # selector 任务两池列均空（与 run_pipeline gate 契约一致）
                    _gate_resolved = True
                elif _gm_a2a == "pool":
                    _pool_eids = _gp_a2a["pool_engine_ids"]
                    _pool_src_a2a = _gp_a2a.get("pool_engine_source", "") or ""
                    _gate_resolved = True
                elif _gf_a2a:
                    log.warning(f"Pipeline emit: scene={_scene_id} agent={agent_type} "
                                f"selector zero-hit, skip enqueue (Issue #1208): {_gf_a2a}")
                    return {"error": _gf_a2a}
            except Exception as _gate_e:
                log.warning(f"Pipeline emit: selector gate failed (fail-open): {_gate_e}")
            if not _gate_resolved:
                # 门禁异常 fail-open：回落既有池解析（无来源标记），广播语义不变
                # 场景 engine 限定广播范围（Issue #259：写入前已按健康过滤）
                from src.domains.runtime.engine.engine_select import get_scene_pool_engine_ids
                _pool_eids = get_scene_pool_engine_ids(
                    _scene_id, agent_type, repo_key=repo_key, user_id=_scene_uid,
                    issue_creator=evt.get("actor", ""))
            if _pool_eids is None:
                # 方案A：场景绑定的引擎全部离线/失联 → 不入队，明确报错
                log.warning(f"Pipeline emit: scene={_scene_id} agent={agent_type} engines all offline (Issue #259)")
                return {"error": f"场景绑定的引擎全部离线（scene={_scene_id}, agent={agent_type}）"}
        elif not runtime_id:
            _picked = pick_engine(repo_key, agent_type, fallback_runtime_id=0)
            if _picked is None:
                return {"error": "no engine available"}
            runtime_id = _picked

        _enqueue_pipeline_task(
            repo_key, seq, agent_type, "", skill_name, evt,
            skill_id, skill_path, runtime_id, issue_id,
            pool_engine_ids=_pool_eids, user_id=_scene_uid if is_plugin_mode() else 0,
            pool_selector=_pool_sel_a2a, pool_engine_source=_pool_src_a2a,
        )
        return {"task_id": "enqueued"}
    except Exception as e:
        return {"error": str(e)[:200]}
