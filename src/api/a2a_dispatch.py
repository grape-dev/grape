"""A2A 事件派发（可被 POST /api/a2a 与排队 drain 共用）。

Issue #1667：排队（queue）语义需要在前序事件终态后重新派发排队事件，
把原 webhook.py 内联的派发逻辑抽到本模块，避免 webhook ↔ event_pipeline
循环依赖，并保证正常路径与排队路径使用同一套派发实现。
"""
from src.config import log


async def _on_handoff_dispatch(event):
    """Event-driven A2A dispatch listener (Phase 4).

    Listens on 'agent/handoff' events emitted by the A2A endpoint.
    Uses pipeline_executor.dispatch_node to route through the configured event mode.
    """
    import asyncio
    from src.api.pipeline_executor import dispatch_node
    from src.config import log

    payload = event.payload or {}
    target_agent = payload.get("target_agent", event.agent_type)
    skill_name = payload.get("skill", "")
    skill_id = payload.get("skill_id", 0)
    skill_path = payload.get("skill_path", "")
    runtime_id = payload.get("runtime_id", 0)
    scene_id = payload.get("scene_id", "default")
    scene_user_id = payload.get("scene_user_id", 0)
    node_id = payload.get("node_id", target_agent)
    evt = payload.get("evt", {})

    log.info(f"agent/handoff listener: dispatching {target_agent} (node={node_id}, scene={scene_id}, uid={scene_user_id})")

    try:
        result = await dispatch_node(
            scene_id=scene_id,
            node_id=node_id,
            repo_key=event.repo_key,
            issue_id=event.issue_id,
            event_seq=event.seq,
            evt=evt,
            skill_name=skill_name,
            skill_id=skill_id,
            skill_path=skill_path,
            runtime_id=runtime_id,
            pool_tag=target_agent,
            user_id=scene_user_id,
        )
        log.info(f"agent/handoff listener: result={result}")
        if result.get("status") == "failed":
            # Issue #476：listener 失败统一走 _fail_event（原裸 update_event_status 无诊断字段）
            _err = result.get("error") or result.get("errors") or "dispatch_node 返回 failed"
            if isinstance(_err, list):
                _err = "; ".join(str(x) for x in _err)
            try:
                from src.api.event_pipeline import _fail_event
                _ref_seq = (payload.get("evt") or {}).get("ref_seq") or payload.get("ref_seq") or 0
                _fail_event(
                    event.seq,
                    f"A2A 交办派发失败（target={target_agent}, node={node_id}）：{str(_err)[:200]}",
                    diag={
                        "code": "a2a_dispatch_failed",
                        "target_stage": target_agent,
                        "target_node": node_id,
                        "phase": "dispatch",
                        "ref_seq": _ref_seq,
                    },
                )
            except Exception as _fe:
                log.warning(f"agent/handoff listener _fail_event failed: {_fe}")
                try:
                    from src.api.event_store import update_event_status
                    update_event_status(event.seq, "failed")
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"agent/handoff listener failed: {e}")
        try:
            from src.api.event_pipeline import _fail_event
            _ref_seq = (payload.get("evt") or {}).get("ref_seq") or payload.get("ref_seq") or 0
            _fail_event(
                event.seq,
                f"A2A 交办派发异常（target={target_agent}, node={node_id}）：{str(e)[:200]}",
                diag={
                    "code": "a2a_dispatch_exception",
                    "target_stage": target_agent,
                    "target_node": node_id,
                    "phase": "exception",
                    "ref_seq": _ref_seq,
                },
            )
        except Exception as _fe2:
            log.warning(f"agent/handoff listener _fail_event failed (exception path): {_fe2}")
            try:
                from src.api.event_store import update_event_status
                update_event_status(event.seq, "failed")
            except Exception:
                pass


def run_a2a_dispatch(repo_key: str, seq: int, agent_name: str, evt: dict,
                     skill_name: str = "", skill_id: int = 0, skill_path: str = "",
                     runtime_id: int = 0, backend: str = "", issue_number="",
                     repository: str = "", ref_seq: int = 0) -> dict:
    """派发已创建的 A2A 事件（plugin 事件总线优先，legacy 派发兜底）。

    Issue #1667：从 webhook.a2a_endpoint 抽出，供正常 A2A 与排队 drain 复用。
    返回与 /api/a2a 兼容的响应字典。
    """
    try:
        from src.api.event_pipeline import is_plugin_mode
        from src.runtime.service import get_execution_mode, EXEC_MODE_DIRECT, EXEC_MODE_REMOTE
        if is_plugin_mode() and get_execution_mode() != EXEC_MODE_DIRECT:
            import asyncio as _aio
            from src.api.event_bus import get_event_bus, GrapeEvent, EventDomain
            from src.api.pipeline_config import get_pipeline_config, get_node_by_tag

            # Determine scene and node config — 用 Issue 级场景，不是仓库级
            scene_id = "default"
            scene_user_id = 0
            try:
                from src.api.session_store import get_issue_scene
                scene_id, scene_user_id = get_issue_scene(repo_key, f"#{issue_number}") if issue_number else ("default", 0)
                scene_id = scene_id or "default"
            except Exception:
                pass

            node = get_node_by_tag(scene_id, agent_name, repo_key=repo_key, user_id=scene_user_id)
            mode = node.get("mode", "emit") if node else "emit"

            handoff_event = GrapeEvent(
                domain=EventDomain.AGENT,
                type="agent/handoff",
                repo_key=repo_key,
                issue_id=f"#{issue_number}" if issue_number else "",
                agent_type=agent_name,
                seq=seq,
                payload={
                    "target_agent": agent_name,
                    "skill": skill_name,
                    "skill_id": skill_id,
                    "skill_path": skill_path,
                    "runtime_id": runtime_id,
                    "mode": mode,
                    "scene_id": scene_id,
                    "scene_user_id": scene_user_id,
                    "node_id": node["id"] if node else agent_name,
                    "ref_seq": ref_seq,
                    "evt": dict(evt, ref_seq=ref_seq),
                },
            )

            # Register the dispatch listener (idempotent — only once)
            bus = get_event_bus()
            if not bus.has_listeners("agent/handoff"):
                bus.on_sync("agent/handoff", _on_handoff_dispatch)

            # Emit the handoff event (async, run in background thread for sync endpoint)
            try:
                loop = _aio.get_event_loop()
            except RuntimeError:
                # 线程池（如排队 drain）无当前事件循环 → 建临时 loop 执行
                loop = _aio.new_event_loop()
            if loop.is_running():
                _aio.ensure_future(bus.emit(handoff_event))
            else:
                _aio.run(bus.emit(handoff_event))

            log.info(f"A2A (plugin): agent/handoff emitted for {agent_name}, seq={seq}, mode={mode}")
            return {"status": "ok", "seq": seq, "agent": agent_name, "mode": "event-driven"}
    except Exception as e:
        log.warning(f"A2A plugin event-driven path failed: {e}, falling back to legacy dispatch")

    # Legacy dispatch (also serves as fallback for plugin mode)
    from src.runtime.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
    # A2A 交办的场景来自 issue_state（智能推荐完成或用户手动选择已持久化）
    _a2a_scene, _a2a_scene_uid = "default", 0
    if issue_number:
        try:
            from src.api.session_store import get_issue_scene
            _a2a_scene, _a2a_scene_uid = get_issue_scene(repo_key, f"#{issue_number}")
            _a2a_scene = _a2a_scene or "default"
        except Exception as _se653:
            # Issue #653：场景解析失败曾是静默回退「default」的旁路（节点查不到 → 全局池），必须留痕
            log.warning(f"A2A: get_issue_scene failed for repo={repo_key} "
                        f"issue=#{issue_number}: {_se653}")
    engine_id = pick_engine(repo_key, agent_name, fallback_runtime_id=runtime_id or 0,
                            scene_id=_a2a_scene, user_id=_a2a_scene_uid)
    if engine_id is None:
        log.info(f"A2A: no available engine for repo={repo_key} agent={agent_name}, seq={seq}, skip")
        # Issue #418：早退统一走 _fail_event，回写 result/exit_code/completed_at
        # （原裸 update_event_status 落库无诊断字段，排障需倒查日志）
        try:
            from src.api.event_pipeline import _fail_event
            _fail_event(seq, f"A2A 派发失败：无可用引擎（pick_engine=None，agent={agent_name}，repo={repository}）")
        except Exception:
            pass
        # a2a-input-validation-fix：派发失败必须如实告知调用方（原先返回 status=ok seq=0，
        # agent 误以为交办成功直接结束任务 → 死机）。失败语义走 failed + error 字段。
        return {"status": "failed", "seq": 0, "agent": agent_name, "error": f"no available engine for agent '{agent_name}' (repo={repository}): 目标引擎不可用，任务未派发，请按失败降级流程处理"}
    # Plugin 模式 + engine_id=0：非 direct 模式均走广播（queue_remote + auto）；仅 direct 走本机 direct
    from src.api.event_pipeline import is_plugin_mode as _is_plugin
    from src.runtime.service import get_execution_mode, EXEC_MODE_DIRECT, EXEC_MODE_REMOTE
    if engine_id == 0 and _is_plugin() and get_execution_mode() != EXEC_MODE_DIRECT:
        from src.api.event_pipeline import _enqueue_pipeline_task
        _be = backend or "opencode"
        _pool_eids = get_scene_pool_engine_ids(_a2a_scene, agent_name, repo_key=repo_key, user_id=_a2a_scene_uid)
        if _pool_eids is None:
            # Issue #259 方案A：场景派发失败 → 不入队，事件置 failed 并明确提示
            # Issue #541：报错区分「场景仓内不存在 / 未绑定引擎 / 引擎全部离线」
            log.warning(f"A2A: scene={_a2a_scene} agent={agent_name} engines all offline, skip (Issue #259)")
            # 引擎绑定真实性同步：改走统一 helper，人话原因落 events.result/agent_tasks.errors
            try:
                from src.api.event_pipeline import _fail_scene_all_offline
                _fail_scene_all_offline(seq, _a2a_scene, agent_name, "A2A", repo_key=repo_key or "")
            except Exception:
                # Issue #418：helper 异常兜底同样走 _fail_event（原裸 update_event_status）
                try:
                    from src.api.event_pipeline import _fail_event
                    _fail_event(seq, f"A2A 派发失败：场景绑定的引擎全部离线（scene={_a2a_scene}，agent={agent_name}，回写降级）")
                except Exception:
                    from src.api.event_store import update_event_status
                    update_event_status(seq, "failed")
            _err541 = "场景绑定的引擎全部离线，任务未派发，请按失败降级流程处理"
            try:
                from src.runtime.engine_select import scene_dispatch_failure_reason
                _err541 = scene_dispatch_failure_reason(
                    _a2a_scene, agent_name, repo_key=repo_key or "", user_id=_a2a_scene_uid) or _err541
            except Exception:
                pass
            return {"status": "failed", "seq": seq, "agent": agent_name, "error": _err541}
        _enqueue_pipeline_task(repo_key, seq, agent_name, _be,
                               skill_name or agent_name, evt,
                               skill_id, skill_path or "", engine_id,
                               f"#{issue_number}" if issue_number else "",
                               pool_engine_ids=_pool_eids, user_id=_a2a_scene_uid)
        log.info(f"A2A (plugin broadcast): enqueued pool task for {agent_name}, seq={seq}")
        return {"status": "ok", "seq": seq, "agent": agent_name, "mode": "broadcast"}
    # backend 取已注册引擎的类型名（不沿用字面默认，engine-selection-design.md v2）
    _be = engine_name(engine_id) or backend
    # engine_id=0：auto/direct 兜底 → 自动选本机在线引擎
    if not engine_id:
        try:
            from src.session_manager import db as _edb, db_lock as _edl
            with _edl:
                _ec = _edb.cursor()
                _ec.execute("SELECT id, name FROM agent_runtimes WHERE status='online' AND healthy=1 ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END LIMIT 1")
                _erow = _ec.fetchone()
            if _erow:
                engine_id = _erow[0]
                _be = _erow[1] or _be
                log.info(f"A2A: auto-selected local engine id={engine_id} name={_be}, seq={seq}")
            else:
                log.info(f"A2A: engine_id=0 no online engine for {agent_name}, seq={seq}, skip")
                # Issue #418：auto-select 空集早退统一走 _fail_event（完整诊断回写）
                try:
                    from src.api.event_pipeline import _fail_event
                    _fail_event(seq, f"A2A 派发失败：无可用引擎（auto-select 空集，无在线本机引擎，agent={agent_name}）")
                except Exception:
                    pass
                return {"status": "ok", "seq": 0, "agent": agent_name}
        except Exception as _ae:
            log.warning(f"A2A: auto-select engine failed: {_ae}")
            # Issue #418：auto-select 异常早退统一走 _fail_event（完整诊断回写）
            try:
                from src.api.event_pipeline import _fail_event
                _fail_event(seq, f"A2A 派发失败：无可用引擎（auto-select 异常: {str(_ae)[:150]}）")
            except Exception:
                pass
            return {"status": "ok", "seq": 0, "agent": agent_name}
    kind = runtime_execution_kind(engine_id)
    if kind == EXEC_MODE_DIRECT:
        from src.api.event_pipeline import start_direct_agent, _create_direct_task, _execute_agent
        _issue_id = f"#{issue_number}" if issue_number else ""
        evt["_skill_id"] = skill_id
        _a2a_task_id = _create_direct_task(repo_key, seq, agent_name, _be,
                            skill_name or agent_name, skill_path or "",
                            engine_id, evt, _issue_id, skill_id=skill_id)
        start_direct_agent(_execute_agent, (
            repo_key, seq, agent_name, _be, skill_name or agent_name, evt, engine_id, _a2a_task_id,
        ), name=f"a2a-{agent_name}-{seq}")
        log.info(f"A2A: started agent execution for {agent_name} (direct, engine={engine_id})")
    else:
        from src.api.event_pipeline import _enqueue_pipeline_task
        _enqueue_pipeline_task(repo_key, seq, agent_name, _be,
                               skill_name or agent_name, evt,
                               skill_id, skill_path, engine_id,
                               f"#{issue_number}" if issue_number else "",
                               user_id=_a2a_scene_uid)
        log.info(f"A2A: enqueued task for {agent_name}, seq={seq}, engine={engine_id}, mode={kind}")
    return {"status": "ok", "seq": seq, "agent": agent_name}