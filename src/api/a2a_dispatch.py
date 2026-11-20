"""A2A 事件派发（可被 POST /api/a2a 与排队 drain 共用）。

Issue #1667：排队（queue）语义需要在前序事件终态后重新派发排队事件，
把原 webhook.py 内联的派发逻辑抽到本模块，避免 webhook ↔ event_pipeline
循环依赖，并保证正常路径与排队路径使用同一套派发实现。
"""
from src.kernel.config import log


async def _on_handoff_dispatch(event):
    """Event-driven A2A dispatch listener (Phase 4).

    Listens on 'agent/handoff' events emitted by the A2A endpoint.
    Uses pipeline_executor.dispatch_node to route through the configured event mode.
    """
    import asyncio
    from src.api.pipeline_executor import dispatch_node
    from src.kernel.config import log

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
                from src.domains.runtime.pipeline.event_pipeline import _fail_event
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
            from src.domains.runtime.pipeline.event_pipeline import _fail_event
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
                     repository: str = "", ref_seq: int = 0,
                     reissue: bool = False, force: bool = False,
                     guard_state: dict | None = None) -> dict:
    """派发已创建的 A2A 事件（plugin 事件总线优先，legacy 派发兜底）。

    Issue #1667：从 webhook.a2a_endpoint 抽出，供正常 A2A 与排队 drain 复用。
    返回与 /api/a2a 兼容的响应字典。

    Issue #2508：投递前执行「事件终态 + 关联任务终态」双检，命中即静默跳过并写
    ``terminal_event_replay`` 审计；``reissue=True``（人工重派）或事件已带
    ``_reissue`` 标记时放行，保留人工恢复能力。

    Issue #2816 复审：终态载体短路由 ``POST /api/a2a`` 门禁执行，而排队事件
    随 drain 直接进入本函数投递，不再读标签/标记行。故在**本函数入口**复查同一
    guard，避免终态标记前入队的 coding 事件在终态后落地。``force=True``（人工
    处置/强制派发）与 ``reissue`` 手动重派同样放行。

    Issue #2816 复审（判断项）：入口 guard 已在 async 端点经 ``to_thread`` 计算过，
    同步调用方无需再次做阻塞 VCS 读。``guard_state`` 为「已预判结果」透传：
    ``None`` = 未预判（drain 等线程内自行判定），``{}`` = 已预判放行，非空 = 命中。
    """
    from src.api.event_store import find_terminal_event_replay, is_event_reissue
    from src.api.pipeline_constants import TERMINAL_EVENT_REPLAY_REASON
    _manual_replay = bool(reissue or is_event_reissue(seq))
    _issue_ref = f"#{str(issue_number).lstrip('#')}" if issue_number else ""
    _evt_payload = (evt or {}).get("payload") or (evt or {}).get("payload_raw") or "{}"
    if isinstance(_evt_payload, str):
        import json as _json_2508
        try:
            _evt_payload = _json_2508.loads(_evt_payload)
        except Exception:
            _evt_payload = {}
    if not isinstance(_evt_payload, dict):
        _evt_payload = {}
    # 载体提示兜底：evt 顶层字段（非仅 payload）也参与显式载体判定，避免排队行
    # 缺 payload 时丢失 pr_number/carrier_pr 优先级。
    if isinstance(evt, dict):
        for _hint_key in ("carrier_pr", "pr_number"):
            if not _evt_payload.get(_hint_key) and evt.get(_hint_key):
                _evt_payload[_hint_key] = evt.get(_hint_key)
    if issue_number and not _manual_replay and not force:
        if guard_state is not None:
            # 调用方已（在 executor 中）完成判定：直接复用，避免事件循环内二次阻塞。
            _terminal = guard_state if isinstance(guard_state, dict) else {}
        else:
            try:
                from src.api.a2a_terminal_guard import guard_terminal_dispatch
                _terminal = guard_terminal_dispatch(
                    repo_key, issue_number, agent_name, _evt_payload)
            except Exception as _te:
                # 判定数据源/依赖异常一律放行（与 guard 内部保守放行同口径）。
                log.warning(f"A2A #2816: terminal guard failed seq={seq}: {_te}")
                _terminal = {}
        if _terminal:
            _t_reason = _terminal.get("reason") or "needs_human_terminal"
            # 排队事件已被 drain 抢占为 running；短路后必须落终态，否则巡检会把
            # 这条永远不会执行的 running 事件当成停滞。
            if int(seq or 0) > 0:
                try:
                    from src.api.event_store import update_event_status
                    update_event_status(
                        int(seq), "cancelled",
                        result=(f"{_t_reason}: terminal carrier guard skipped "
                                f"dispatch (carrier_pr={_terminal.get('carrier_pr') or 0})"))
                except Exception as _ue:
                    log.warning(f"A2A #2816: mark skipped event terminal failed seq={seq}: {_ue}")
            from src.api.terminal_disposition import available_actions
            log.info(f"A2A terminal guard (dispatch entry): skip seq={seq} "
                     f"agent={agent_name} issue=#{issue_number} reason={_t_reason}")
            return {"status": "skipped", "seq": int(seq or 0), "agent": agent_name,
                    "skipped": _t_reason, "queued": False,
                    "reason": f"{_t_reason}:{_terminal.get('detail') or ''}",
                    "triggers": _terminal.get("triggers") or [],
                    "carrier_pr": _terminal.get("carrier_pr") or 0,
                    "folded": bool(_terminal.get("folded")),
                    "conflict_scope": _terminal.get("conflict_scope") or "",
                    "manual_status": _terminal.get("manual_status") or "",
                    "manual_applied": bool(_terminal.get("manual_applied")),
                    "manual_actions": available_actions(_terminal),
                    "handoff_result": "rejected",
                    "message": ("issue is folded into a terminal carrier PR "
                                "(needs-human/needs-human-terminal); coding dispatch skipped "
                                "and the issue moved to a manual state. Use the terminal "
                                "disposition entry (new_carrier/reassign/close) or force=true "
                                "to override manually.")}
    if not _manual_replay:
        # Issue #2508 review: the fingerprint alone is identical for every
        # handoff to the same node, so the gate also compares the request
        # content (context + pr_number) before blocking anything.
        from src.api.event_store import _content_key as _content_key_fn
        _replay = find_terminal_event_replay(
            repo_key, event_seq=seq,
            idempotency_key=str((evt or {}).get("idempotency_key") or ""),
            fingerprint=str((evt or {}).get("fingerprint") or ""),
            agent_type=agent_name, issue_id=_issue_ref,
            content_key=_content_key_fn(_evt_payload))
        if _replay:
            from src.api.a2a_dispatch_guard import record_terminal_replay_skip
            _detail = record_terminal_replay_skip(repo_key, issue_number, agent_name, _replay)
            log.info(f"A2A: {TERMINAL_EVENT_REPLAY_REASON} skipped seq={seq} agent={agent_name} {_detail}")
            return {"status": "skipped", "seq": int(seq or 0), "agent": agent_name,
                    "skipped": TERMINAL_EVENT_REPLAY_REASON,
                    "reason": f"{TERMINAL_EVENT_REPLAY_REASON}:{_replay.get('seq', seq)}",
                    "original_event": _replay}
    try:
        from src.domains.runtime.pipeline.event_pipeline import is_plugin_mode
        from src.domains.runtime.engine.service import get_execution_mode, EXEC_MODE_DIRECT, EXEC_MODE_REMOTE
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
    from src.domains.runtime.engine.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
    from src.domains.runtime.engine.service import runtime_execution_kind, EXEC_MODE_DIRECT
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
            from src.domains.runtime.pipeline.event_pipeline import _fail_event
            _fail_event(seq, f"A2A 派发失败：无可用引擎（pick_engine=None，agent={agent_name}，repo={repository}）")
        except Exception:
            pass
        # a2a-input-validation-fix：派发失败必须如实告知调用方（原先返回 status=ok seq=0，
        # agent 误以为交办成功直接结束任务 → 死机）。失败语义走 failed + error 字段。
        return {"status": "failed", "seq": 0, "agent": agent_name, "error": f"no available engine for agent '{agent_name}' (repo={repository}): 目标引擎不可用，任务未派发，请按失败降级流程处理"}
    # Plugin 模式 + engine_id=0：非 direct 模式均走广播（queue_remote + auto）；仅 direct 走本机 direct
    from src.domains.runtime.pipeline.event_pipeline import is_plugin_mode as _is_plugin
    from src.domains.runtime.engine.service import get_execution_mode, EXEC_MODE_DIRECT, EXEC_MODE_REMOTE
    if engine_id == 0 and _is_plugin() and get_execution_mode() != EXEC_MODE_DIRECT:
        from src.domains.runtime.pipeline.event_pipeline import _enqueue_pipeline_task
        _be = backend or "opencode"
        _pool_eids = get_scene_pool_engine_ids(_a2a_scene, agent_name, repo_key=repo_key, user_id=_a2a_scene_uid)
        if _pool_eids is None:
            # Issue #259 方案A：场景派发失败 → 不入队，事件置 failed 并明确提示
            # Issue #541：报错区分「场景仓内不存在 / 未绑定引擎 / 引擎全部离线」
            log.warning(f"A2A: scene={_a2a_scene} agent={agent_name} engines all offline, skip (Issue #259)")
            # 引擎绑定真实性同步：改走统一 helper，人话原因落 events.result/agent_tasks.errors
            try:
                from src.domains.runtime.pipeline.event_pipeline import _fail_scene_all_offline
                _fail_scene_all_offline(seq, _a2a_scene, agent_name, "A2A", repo_key=repo_key or "")
            except Exception:
                # Issue #418：helper 异常兜底同样走 _fail_event（原裸 update_event_status）
                try:
                    from src.domains.runtime.pipeline.event_pipeline import _fail_event
                    _fail_event(seq, f"A2A 派发失败：场景绑定的引擎全部离线（scene={_a2a_scene}，agent={agent_name}，回写降级）")
                except Exception:
                    from src.api.event_store import update_event_status
                    update_event_status(seq, "failed")
            _err541 = "场景绑定的引擎全部离线，任务未派发，请按失败降级流程处理"
            try:
                from src.domains.runtime.engine.engine_select import scene_dispatch_failure_reason
                _err541 = scene_dispatch_failure_reason(
                    _a2a_scene, agent_name, repo_key=repo_key or "", user_id=_a2a_scene_uid) or _err541
            except Exception:
                pass
            return {"status": "failed", "seq": seq, "agent": agent_name, "error": _err541}
        # [#1208 A2A selector 修复 2026-11-12] 兜底广播分支补三分叉 gate（同 run_pipeline 口径）
        _pool_sel_a2a = ""
        try:
            from src.domains.runtime.pipeline.event_pipeline import _scene_pool_gate
            _gm_a2a, _gp_a2a, _gf_a2a = _scene_pool_gate(
                _a2a_scene, agent_name, repo_key=repo_key, user_id=_a2a_scene_uid)
            if _gm_a2a == "selector":
                _pool_sel_a2a = _gp_a2a["pool_selector"]
                _pool_eids = ""
            elif _gf_a2a:
                log.warning(f"A2A: scene={_a2a_scene} agent={agent_name} selector zero-hit, "
                            f"skip enqueue (Issue #1208): {_gf_a2a}")
                try:
                    from src.api.event_pipeline import _fail_event
                    _fail_event(seq, _gf_a2a)
                except Exception:
                    from src.api.event_store import update_event_status
                    update_event_status(seq, "failed")
                return {"status": "failed", "seq": seq, "agent": agent_name, "error": _gf_a2a}
        except Exception as _gate_e:
            log.warning(f"A2A: selector gate failed (fail-open): {_gate_e}")
        _bcast_task_id = _enqueue_pipeline_task(repo_key, seq, agent_name, _be,
                               skill_name or agent_name, evt,
                               skill_id, skill_path or "", engine_id,
                               f"#{issue_number}" if issue_number else "",
                               pool_engine_ids=_pool_eids, user_id=_a2a_scene_uid,
                               pool_selector=_pool_sel_a2a)
        # Issue #3201：接管守门命中时入队返回空串 → 按 skipped 返回，不得误报 ok。
        if not _bcast_task_id:
            log.info(f"A2A (plugin broadcast): skipped {agent_name}, seq={seq} "
                     f"(reason=already_taken_over)")
            return {"status": "skipped", "seq": seq, "agent": agent_name,
                    "reason": "already_taken_over"}
        log.info(f"A2A (plugin broadcast): enqueued pool task for {agent_name}, seq={seq}")
        return {"status": "ok", "seq": seq, "agent": agent_name, "mode": "broadcast"}
    # backend 取已注册引擎的类型名（不沿用字面默认，engine-selection-design.md v2）
    _be = engine_name(engine_id) or backend
    # engine_id=0：auto/direct 兜底 → 自动选本机在线引擎
    if not engine_id:
        try:
            from src.domains.runtime import public as _rt_eng
            _erow = _rt_eng.first_online_runtime()
            if _erow:
                engine_id = _erow[0]
                _be = _erow[1] or _be
                log.info(f"A2A: auto-selected local engine id={engine_id} name={_be}, seq={seq}")
            else:
                log.info(f"A2A: engine_id=0 no online engine for {agent_name}, seq={seq}, skip")
                # Issue #418：auto-select 空集早退统一走 _fail_event（完整诊断回写）
                try:
                    from src.domains.runtime.pipeline.event_pipeline import _fail_event
                    _fail_event(seq, f"A2A 派发失败：无可用引擎（auto-select 空集，无在线本机引擎，agent={agent_name}）")
                except Exception:
                    pass
                return {"status": "ok", "seq": 0, "agent": agent_name}
        except Exception as _ae:
            log.warning(f"A2A: auto-select engine failed: {_ae}")
            # Issue #418：auto-select 异常早退统一走 _fail_event（完整诊断回写）
            try:
                from src.domains.runtime.pipeline.event_pipeline import _fail_event
                _fail_event(seq, f"A2A 派发失败：无可用引擎（auto-select 异常: {str(_ae)[:150]}）")
            except Exception:
                pass
            return {"status": "ok", "seq": 0, "agent": agent_name}
    kind = runtime_execution_kind(engine_id)
    if kind == EXEC_MODE_DIRECT:
        from src.domains.runtime.pipeline.event_pipeline import start_direct_agent, _create_direct_task, _execute_agent
        _issue_id = f"#{issue_number}" if issue_number else ""
        evt["_skill_id"] = skill_id
        _a2a_task_id = _create_direct_task(repo_key, seq, agent_name, _be,
                            skill_name or agent_name, skill_path or "",
                            engine_id, evt, _issue_id, skill_id=skill_id)
        # Issue #3201：守门命中时 _create_direct_task 返回空串——不得继续 start_direct_agent
        # （task_id="" 会让 _execute_agent_inner 无 task 行照常 build prompt / 跑 agent，
        # 与本地接管双写 worktree），按 skipped 如实返回，交给 drain 视作「已跳过」而非失败。
        if not _a2a_task_id:
            log.info(f"A2A: skipped direct dispatch for {agent_name}, seq={seq} "
                     f"(reason=already_taken_over)")
            return {"status": "skipped", "seq": seq, "agent": agent_name,
                    "reason": "already_taken_over"}
        start_direct_agent(_execute_agent, (
            repo_key, seq, agent_name, _be, skill_name or agent_name, evt, engine_id, _a2a_task_id,
        ), name=f"a2a-{agent_name}-{seq}")
        log.info(f"A2A: started agent execution for {agent_name} (direct, engine={engine_id})")
    else:
        from src.domains.runtime.pipeline.event_pipeline import _enqueue_pipeline_task
        _remote_task_id = _enqueue_pipeline_task(repo_key, seq, agent_name, _be,
                               skill_name or agent_name, evt,
                               skill_id, skill_path, engine_id,
                               f"#{issue_number}" if issue_number else "",
                               user_id=_a2a_scene_uid)
        # Issue #3201：接管守门命中时入队返回空串 → 按 skipped 返回；drain 视 skipped 为
        # 已跳过（seq 非空、非 failed），不会误走 _fail_event 把事件改成 failed。
        if not _remote_task_id:
            log.info(f"A2A: skipped remote dispatch for {agent_name}, seq={seq} "
                     f"(reason=already_taken_over)")
            return {"status": "skipped", "seq": seq, "agent": agent_name,
                    "reason": "already_taken_over"}
        log.info(f"A2A: enqueued task for {agent_name}, seq={seq}, engine={engine_id}, mode={kind}")
    return {"status": "ok", "seq": seq, "agent": agent_name}
