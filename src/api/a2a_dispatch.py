"""A2A 事件派发（可被 POST /api/a2a 与排队 drain 共用）。

Issue #1667：排队（queue）语义需要在前序事件终态后重新派发排队事件，
把原 webhook.py 内联的派发逻辑抽到本模块，避免 webhook ↔ event_pipeline
循环依赖，并保证正常路径与排队路径使用同一套派发实现。
"""
import json

from src.kernel.config import log
from src.kernel.issue_ident import is_actionable_issue_number, native_issue_number


def _resolve_event_payload(event, evt) -> dict:
    """取 listener 事件对应的持久化 payload（优先 DB，兜底嵌套 evt.payload）。

    Issue #2118 复审：`run_a2a_dispatch` 在 plugin/queue_remote 路径只把 handoff
    入队就返回，真正失败由本 listener 后置处理。此时复核 stage_guard／是否为聚合
    重排事件必须以持久化 payload 为准（嵌套 evt 只是快照，可能缺后续写入字段）。
    """
    dragged = {}
    if isinstance(evt, dict) and isinstance(evt.get("payload"), dict):
        dragged = dict(evt["payload"])
    seq = int(getattr(event, "seq", 0) or 0)
    if not seq:
        return dragged
    try:
        from src.domains.runtime import public as _runtime_public

        loaded = _runtime_public.event_payload(seq)
        if loaded:
            dragged.update(loaded)
    except Exception as exc:
        log.warning(f"agent/handoff listener: load event payload failed seq={seq}: {exc}")
    return dragged


def _advance_auto_advance_stage(event, evt) -> None:
    """Issue #2118 复审：自动推进事件在下游确认接受派发后才推进 stage。

    仅当事件携带 auto-advance stage_guard，且当前 stage 仍等于 guard.from
    （调用方未提前写）时，才把 stage 推进到 guard.to；人工/其它路径已改 stage 时
    不覆盖。失败路径不在此处理，由 `_fail_event` 统一留痕并保持原 stage。
    """
    payload = _resolve_event_payload(event, evt)
    guard = payload.get("stage_guard") if isinstance(payload, dict) else None
    if not isinstance(guard, dict):
        return
    from_stage = str(guard.get("from") or "").strip()
    to_stage = str(guard.get("to") or "").strip()
    repo_key = getattr(event, "repo_key", "") or ""
    issue_id = getattr(event, "issue_id", "") or ""
    if not from_stage or not to_stage or from_stage == to_stage or not repo_key or not issue_id:
        return
    try:
        from src.api.session_store import get_issue_stage, set_issue_stage
        cur = (get_issue_stage(repo_key, issue_id) or {}).get("stage") or ""
        if cur != from_stage:
            log.info(f"agent/handoff listener: auto-advance stage not '{from_stage}' "
                     f"(now='{cur}'), skip advance seq={getattr(event, 'seq', 0)}")
            return
        set_issue_stage(repo_key, issue_id, to_stage, to_stage, a2a_seq=getattr(event, "seq", 0))
        log.info(f"agent/handoff listener: auto-advance stage {from_stage}->{to_stage} "
                 f"issue={issue_id} seq={getattr(event, 'seq', 0)}")
    except Exception as exc:
        log.warning(f"agent/handoff listener: auto-advance stage failed "
                    f"issue={issue_id}: {exc}")


def _record_dispatch_not_confirmed(event, payload: dict, evt, target_agent: str,
                                   node_id: str, reason: str, code: str,
                                   phase: str) -> None:
    """listener 未确认成功派发（failed/blocked/异常）的统一收口。

    Issue #2118 复审（Critical）：plugin/queue_remote 路径下 `run_a2a_dispatch`
    只代表「handoff 已发/已入队」，真正的 `dispatch_node` 结果只在 listener 可见。
    聚合重排事件（payload.aggregate_recheck）失败时必须走
    `_requeue_or_degrade_aggregate_recheck`，否则退避重排/耗尽转 pending_review
    的承诺在异步路径失效，组织 Issue 会静默停在 qa。其余事件仍走 `_fail_event`
    留痕并保持/回滚 stage。
    """
    seq = int(getattr(event, "seq", 0) or 0)
    repo_key = getattr(event, "repo_key", "") or ""
    issue_id = getattr(event, "issue_id", "") or ""
    state = _resolve_event_payload(event, evt)
    if state.get("aggregate_recheck"):
        try:
            from src.domains.runtime.pipeline.event_pipeline import (
                _requeue_or_degrade_aggregate_recheck,
            )
            _requeue_or_degrade_aggregate_recheck(
                seq, repo_key,
                target_agent or getattr(event, "agent_type", "") or "",
                issue_id, state, reason, code,
            )
            return
        except Exception as exc:
            log.warning(f"agent/handoff listener: aggregate recheck requeue failed "
                        f"seq={seq}: {exc}")
    try:
        from src.domains.runtime.pipeline.event_pipeline import _fail_event
        _ref_seq = ((payload or {}).get("evt") or {}).get("ref_seq") \
            or (payload or {}).get("ref_seq") or 0
        _fail_event(
            seq, reason,
            diag={
                "code": code,
                "target_stage": target_agent,
                "target_node": node_id,
                "phase": phase,
                "ref_seq": _ref_seq,
            },
        )
    except Exception as exc:
        log.warning(f"agent/handoff listener _fail_event failed: {exc}")
        try:
            from src.api.event_store import update_event_status
            update_event_status(seq, "failed")
        except Exception:
            pass


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
        status = str((result or {}).get("status") or "").lower()
        if status == "failed":
            # Issue #476：listener 失败统一走 _fail_event（原裸 update_event_status 无诊断字段）
            _err = result.get("error") or result.get("errors") or "dispatch_node 返回 failed"
            if isinstance(_err, list):
                _err = "; ".join(str(x) for x in _err)
            _record_dispatch_not_confirmed(
                event, payload, evt, target_agent, node_id,
                f"A2A 交办派发失败（target={target_agent}, node={node_id}）：{str(_err)[:200]}",
                "a2a_dispatch_failed", "dispatch",
            )
        elif status == "dispatched":
            # Issue #2118 复审：自动推进事件在 plugin 事件驱动路径下，run_a2a_dispatch
            # 只因「handoff 事件已发」就返回 ok；真正的 dispatch_node 结果只在 listener
            # 可见。因此 stage 推进必须落在这里（确认下游已接受派发），而非调用方提前写。
            _advance_auto_advance_stage(event, evt)
        else:
            # blocked/未知：节点未实际派发，绝不能推进 stage（否则 stage 指向从未
            # 运行的下游）；统一留痕，聚合重排事件仍按退避重排/降级。
            log.warning(f"agent/handoff listener: dispatch not confirmed "
                        f"status={status or 'unknown'} target={target_agent} node={node_id}")
            _record_dispatch_not_confirmed(
                event, payload, evt, target_agent, node_id,
                f"A2A 交办未确认派发（target={target_agent}, node={node_id}, "
                f"status={status or 'unknown'}）：{str(result)[:150]}",
                "a2a_dispatch_blocked", "dispatch",
            )
    except Exception as e:
        log.warning(f"agent/handoff listener failed: {e}")
        # Issue #2118 复审（Critical）：异常路径只调用共享收口，由
        # _record_dispatch_not_confirmed 内部决定「重排」或「终态失败」。
        # 此前在收口之后又无条件 _fail_event，会把刚写回的 queued 重排事件
        # 立即覆盖为 failed（event_store 终态保护只拦终态覆盖终态，不拦
        # queued->failed），导致退避重排与耗尽转 pending_review 全部失效。
        _record_dispatch_not_confirmed(
            event, payload, evt, target_agent, node_id,
            f"A2A 交办派发异常（target={target_agent}, node={node_id}）：{str(e)[:200]}",
            "a2a_dispatch_exception", "exception",
        )


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
            # async=True：仅表示 handoff 事件已发出，dispatch_node 结果稍后由 listener
            # 决定。自动推进调用方据此推迟 stage 写入（Issue #2118 复审），避免
            # listener 后置 failed 时 stage 已指向从未运行的下游。
            return {"status": "ok", "seq": seq, "agent": agent_name,
                    "mode": "event-driven", "async": True}
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
        _enq_args = (
            repo_key, seq, agent_name, _be, skill_name or agent_name, evt,
            skill_id, skill_path or "", engine_id,
            f"#{issue_number}" if issue_number else "",
        )
        try:
            _enqueue_pipeline_task(
                *_enq_args,
                pool_engine_ids=_pool_eids, user_id=_a2a_scene_uid,
                pool_selector=_pool_sel_a2a,
            )
        except TypeError as _te:
            # 混合版本滚动升级：a2a_dispatch 已带 pool_selector，但进程内 event_pipeline
            # 仍是旧签名。selector 为空时降级为普通入队；非空 selector 不能丢语义，继续抛。
            if "pool_selector" not in str(_te) or _pool_sel_a2a:
                raise
            log.warning(
                "A2A: enqueue module does not accept pool_selector; "
                "fallback without selector (mixed-version process)")
            _enqueue_pipeline_task(
                *_enq_args,
                pool_engine_ids=_pool_eids, user_id=_a2a_scene_uid,
            )
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
        start_direct_agent(_execute_agent, (
            repo_key, seq, agent_name, _be, skill_name or agent_name, evt, engine_id, _a2a_task_id,
        ), name=f"a2a-{agent_name}-{seq}")
        log.info(f"A2A: started agent execution for {agent_name} (direct, engine={engine_id})")
    else:
        from src.domains.runtime.pipeline.event_pipeline import _enqueue_pipeline_task
        _enqueue_pipeline_task(repo_key, seq, agent_name, _be,
                               skill_name or agent_name, evt,
                               skill_id, skill_path, engine_id,
                               f"#{issue_number}" if issue_number else "",
                               user_id=_a2a_scene_uid)
        log.info(f"A2A: enqueued task for {agent_name}, seq={seq}, engine={engine_id}, mode={kind}")
    return {"status": "ok", "seq": seq, "agent": agent_name}


# ===== Issue #2251: 组织下派终态 → 幂等触发 org_aggregate（aggregate_recheck）=====

def trigger_org_aggregate(dispatch: dict, targets: list[dict] | None,
                          terminal_status: str) -> dict:
    """批次进入终态时幂等持久化一条 org_aggregate 重排事件。

    - 幂等键：``org_agg:<dispatch_id>:<terminal_status>``（events 唯一索引兜底，
      重复 sweep / 多实例并发都只落一条）；
    - 事件以 ``status='queued'`` 持久化（``aggregate_recheck=true``），由巡检
      ``reconcile_queued_a2a`` 按 ``_not_before`` 派发，进程重启不丢；
    - 派发失败沿用 #2118 的退避重排 / 耗尽转 pending_review 路径。

    返回 ``{"status": "queued"|"dedup"|"skipped", "seq": N}``。
    """
    from datetime import datetime, timezone

    from src.api.event_store import append_event, find_event_by_idempotency_key
    from src.config import log
    from src.kernel.repo_key import from_repo_key

    repo_key = str((dispatch or {}).get("org_repo_key") or "")
    # D105/A12：org issue 编号是平台原生 ident（数字或 Gitee IK* 字符串），
    # 不能用 int() 收口；dispatch.id 是 TEXT uuid，保持字符串。
    issue_number = native_issue_number((dispatch or {}).get("org_issue_number") or "")
    dispatch_id = str((dispatch or {}).get("id") or "")
    scene_id = str((dispatch or {}).get("scene_id") or "") or "default"
    if not repo_key or not is_actionable_issue_number(issue_number) or not dispatch_id:
        return {"status": "skipped", "seq": 0}

    idem = f"org_agg:{dispatch_id}:{str(terminal_status or '')}"[:128]
    existing = find_event_by_idempotency_key(repo_key, idem)
    if existing:
        return {"status": "dedup", "seq": int(existing.get("seq") or 0)}

    from src.api.pipeline_config import get_pipeline_config, get_pipeline_transition, node_role
    transition = get_pipeline_transition(scene_id, "org_aggregate", repo_key=repo_key) or {}
    node = transition.get("node") or {}
    if not node:
        # 自定义组织场景可能给聚合节点换了 id/tag，但技能语义仍是 org-aggregate-cli。
        # 按技能兜底解析，避免聚合事件没有目标技能而永远派发不出去。
        _cfg = get_pipeline_config(scene_id, repo_key=repo_key) or {}
        node = next(
            (n for n in (_cfg.get("nodes") or [])
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
    _evt = {
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
            "evt": _evt,
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
    except Exception as exc:
        log.warning(f"org-aggregate: persist recheck event failed dispatch={dispatch_id}: {exc}")
        return {"status": "skipped", "seq": 0}
    # Issue #2270：批次终态时才触发聚合节点，这里同步把组织 Issue stage 推进到
    # 聚合角色（qa）。此前依赖 org_dispatch 节点提前 A2A 来推进 stage；改为「等
    # 批次终态」后，必须由触发方负责 stage，否则聚合任务完成时 stage 仍是 coding，
    # auto-advance 会因 stage 不匹配而跳过 finalize/pending_review。
    try:
        from src.api.session_store import set_issue_stage
        set_issue_stage(repo_key, f"#{issue_number}", role, "org_aggregate")
    except Exception as exc:
        log.warning(f"org-aggregate: set issue stage failed issue=#{issue_number}: {exc}")
    if seq <= 0:
        # 撞幂等键：已有事件（负 seq），不再触发
        return {"status": "dedup", "seq": abs(int(seq))}
    log.info(f"org-aggregate: queued recheck event seq={seq} dispatch={dispatch_id} "
             f"status={terminal_status} role={role} scene={scene_id}")
    return {"status": "queued", "seq": int(seq)}
