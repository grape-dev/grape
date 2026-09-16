"""Events 路由：事件列表、详情、Event 对话（event-conversation-design.md）"""
import asyncio
import os
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel
from src.api.deps import get_current_user_or_api_key, require_no_session_token
from src.config import log
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/detail/{seq}", operation_id="events_detail", summary="事件详情（结果/日志）")
def get_event_detail(seq: int, user: dict = Depends(get_current_user_or_api_key)):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, repo_key, issue_id, event_type, source, agent_type, title, actor, summary, payload, status, created_at, runtime, skill, exit_code, duration, agent_logs, started_at, completed_at, result FROM events WHERE seq=?",
                (seq,)
            )
            row = c.fetchone()
        except Exception:
            row = None

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件未找到"})

    # 权限校验：用户必须有权访问该事件所属仓库
    from src.api.deps import check_repo_access
    check_repo_access(user, row[1] or "")

    import json
    from src.repo_key import from_repo_key
    payload = {}
    try:
        payload = json.loads(row[9]) if row[9] else {}
    except Exception:
        pass

    agent_logs = []
    try:
        agent_logs = json.loads(row[16]) if row[16] else []
    except Exception:
        pass

    result = {
        "seq": row[0],
        "repo_key": row[1] or "",
        "issue_id": row[2],
        "event_type": row[3],
        "source": row[4],
        "agent_type": row[5] or "",
        "title": row[6] or row[3],
        "actor": row[7] or "",
        "summary": row[8] or row[6] or row[3],
        "payload": payload,
        "status": row[10],
        "created_at": normalize_event_time(row[11]),
        "runtime": row[12] or "",
        "skill": row[13] or "",
        "exit_code": row[14],
        "duration": row[15],
        "agent_logs": agent_logs,
        "started_at": normalize_event_time(row[17] or ""),
        "completed_at": normalize_event_time(row[18] or ""),
        "result": row[19] or "",
    }
    # 前后同 agent_type 事件 seq（供前端箭头导航）
    try:
        agent_type = row[5] or ""
        with db_lock:
            c2 = db.cursor()
            c2.execute("SELECT MAX(seq) FROM events WHERE seq < ? AND agent_type=?", (seq, agent_type))
            prev_seq = c2.fetchone()[0]
            c2.execute("SELECT MIN(seq) FROM events WHERE seq > ? AND agent_type=?", (seq, agent_type))
            next_seq = c2.fetchone()[0]
        result["prev_seq"] = prev_seq if prev_seq else 0
        result["next_seq"] = next_seq if next_seq else 0
    except Exception:
        result["prev_seq"] = 0
        result["next_seq"] = 0
    # M4：附 event.json archive 对应 pipeline 段（结构化产出展示，见 §11.4）
    try:
        import re as _re
        num = ""
        if row[2]:
            m = _re.search(r"(\d+)", str(row[2]))
            if m:
                num = m.group(1)
        if num:
            from src.api.event_archive import load_event_json
            arch = load_event_json(row[1] or "", num)
            if arch:
                for seg in arch.get("pipeline", []):
                    if seg.get("seq") == row[0]:
                        result["archive"] = seg
                        break
                if "archive" not in result:
                    result["archive"] = arch
    except Exception:
        pass
    # 引擎实例名（events.runtime 是引擎类型名；具体实例多级回退解析，见 agent-cluster-design.md）
    # 1) agent_tasks.runtime_id（queue/池模式实际执行引擎）
    # 2) devops_agent_session.runtime_id（direct 模式绑定实例）
    # 3) events.runtime 类型名 → 本机/在线实例
    engine_instance = ""
    engine_device = ""
    engine_ip = ""
    runtime_id = 0
    try:
        from src.runtime.instances import instance_name
        runtime_id = 0
        with db_lock:
            c2 = db.cursor()
            c2.execute(
                "SELECT runtime_id FROM agent_tasks WHERE event_seq=? AND runtime_id IS NOT NULL AND runtime_id>0 "
                "ORDER BY created_at DESC LIMIT 1",
                (seq,))
            tr = c2.fetchone()
            if tr:
                runtime_id = tr[0]
            else:
                # direct 模式：查该 repo+agent 会话绑定的实例
                c2.execute(
                    "SELECT runtime_id FROM devops_agent_session WHERE repo_key=? AND agent_type=? AND runtime_id>0 LIMIT 1",
                    (row[1] or "", row[5] or ""))
                sr = c2.fetchone()
                if sr:
                    runtime_id = sr[0]
            if runtime_id:
                c2.execute(
                    "SELECT ar.name, ar.credential_id, COALESCE(rc.device_name,''), COALESCE(rc.last_ip,'') "
                    "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id=ar.credential_id WHERE ar.id=?",
                    (runtime_id,))
                ar = c2.fetchone()
                if ar:
                    engine_instance = instance_name(ar[0] or "engine", ar[2] or "", ar[1] or 0)
                    engine_device = ar[2] or ""
                    engine_ip = ar[3] or ""
            if not engine_instance and row[12]:
                # 兜底：按类型名找本机（credential_id=0）优先、其次在线实例
                c2.execute(
                    "SELECT ar.id, ar.name, ar.credential_id, COALESCE(rc.device_name,''), COALESCE(rc.last_ip,'') "
                    "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id=ar.credential_id "
                    "WHERE ar.name=? ORDER BY (ar.credential_id=0) DESC, ar.status='online' DESC, ar.id ASC LIMIT 1",
                    (row[12],))
                ar2 = c2.fetchone()
                if ar2:
                    engine_instance = instance_name(ar2[1] or "engine", ar2[3] or "", ar2[2] or 0)
                    engine_device = ar2[3] or ""
                    engine_ip = ar2[4] or ""
    except Exception:
        pass
    result["engine_instance"] = engine_instance
    result["engine_device"] = engine_device
    result["engine_ip"] = engine_ip

    # 引擎健康分（Issue #134 展示增强）
    engine_health = {"score": None, "total_tasks": 0, "failed_tasks": 0,
                     "consecutive_failures": 0, "heartbeat_alive": None}
    if runtime_id:
        try:
            from src.runtime.health_scorer import get_health_score
            hd = get_health_score(runtime_id)
            engine_health = {
                "score": hd.get("health_score"),
                "total_tasks": hd.get("total_tasks", 0),
                "failed_tasks": hd.get("failed_tasks", 0),
                "consecutive_failures": hd.get("consecutive_failures", 0),
                "heartbeat_alive": hd.get("heartbeat_alive"),
            }
        except Exception:
            pass
    result["engine_health"] = engine_health

    # 关联任务（重派按钮展示/判定用，见 event-redispatch-design.md §4.1.1）
    task_info = {"task_id": "", "task_status": "", "runtime_id": 0,
                 "runtime_session_id": "", "task_created_at": "", "task_started_at": ""}
    try:
        with db_lock:
            c4 = db.cursor()
            c4.execute(
                "SELECT id, status, runtime_id, runtime_session_id, created_at, started_at "
                "FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1",
                (seq,))
            tr3 = c4.fetchone()
            if tr3:
                task_info = {
                    "task_id": tr3[0], "task_status": tr3[1], "runtime_id": tr3[2] or 0,
                    "runtime_session_id": tr3[3] or "", "task_created_at": tr3[4] or "",
                    "task_started_at": tr3[5] or "",
                }
    except Exception:
        pass
    result["task"] = task_info

    return result


@router.post("/{seq}/terminate")
def terminate_event(seq: int, user: dict = Depends(require_no_session_token)):
    """强行终止正在运行的事件任务。

    - worker 模式（runtime_id 绑定远端 worker）→ cancel_task（task 状态=cancelled）
      worker 心跳 15s 内匹配 WHERE status=cancelled → _kill_stale_engine 杀引擎进程
    - direct 模式（本机引擎）→ fail_task（task 状态=failed）+ cancel_direct_task 杀子进程
    - 事件状态回写 failed
    """
    from src.session_manager import db, db_lock
    from src.api.event_store import update_event_status

    # 查事件状态（含 repo_key 供归属校验）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, repo_key FROM events WHERE seq=?", (seq,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件不存在"})
    evt_status = row[0]
    from src.api.deps import check_repo_access
    check_repo_access(user, row[1] or "")

    if evt_status not in ('running', 'dispatched'):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"事件状态为 {evt_status}，无法终止"})

    # 查关联 task_id（agent_tasks 表按 event_seq 关联）
    task_id = ""
    try:
        with db_lock:
            c2 = db.cursor()
            c2.execute("SELECT id FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1", (seq,))
            tr = c2.fetchone()
            if tr:
                task_id = tr[0]
    except Exception:
        pass

    cancelled = False
    # 1) 有关联 task_id → 根据 execution mode 分流：
    #    worker 模式 → cancel_task（标记 cancelled，worker 心跳匹配后立即杀引擎）
    #    direct 模式 → fail_task（标记 failed，cancel_direct_task 杀本地子进程）
    if task_id:
        try:
            from src.runtime.service import (
                get_task, cancel_task, fail_task,
                runtime_execution_kind, EXEC_MODE_REMOTE,
            )
            _task = get_task(task_id)
            _is_worker = (
                _task
                and runtime_execution_kind(_task.get("runtime_id") or 0) == EXEC_MODE_REMOTE
            )
            if _is_worker:
                cancel_task(task_id)
                # 事件档案/状态收尾（cancel_task 只更新 task 状态，事件侧需手动补）
                if _task.get("event_seq"):
                    try:
                        from datetime import datetime, timezone
                        from src.api.event_store import update_event_complete
                        from src.api.event_archive import finalize_event_archive
                        finalize_event_archive(
                            _task.get("repo_key", ""), _task["event_seq"], "failed",
                            exit_code=-1,
                            tokens=[_task.get("repo_gitcode_token") or ""],
                            issue_num_hint=_task.get("issue_id") or "",
                        )
                        update_event_complete(
                            _task["event_seq"], "failed", exit_code=-1,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                            result="用户手动终止",
                        )
                    except Exception as e:
                        log.warning(f"Event terminate: worker cancel event finalize failed: {e}")
                cancelled = True
                log.info(f"Event terminate: seq={seq} task={task_id} cancelled via cancel_task (worker mode)")
            else:
                fail_task(task_id, "用户手动终止")
                cancelled = True
                log.info(f"Event terminate: seq={seq} task={task_id} failed via fail_task (direct mode)")
        except Exception as e:
            log.warning(f"Event terminate: task cancel/fail failed: {e}")

    # 2) direct 模式 → cancel_direct_task（无论有无 task_id 都尝试，兜底）
    #    同时取消 worker 模式的 future（direct 模式杀本地子进程）
    try:
        from src.api.event_pipeline import cancel_direct_task
        dc = cancel_direct_task(seq)
        if dc:
            cancelled = True
            log.info(f"Event terminate: seq={seq} direct cancel={dc}")
    except Exception as e:
        log.warning(f"Event terminate: cancel_direct_task failed: {e}")

    # 3) 事件状态回写 failed（兜底，fail_task/cancel_task 已处理但再确认一次）
    try:
        update_event_status(seq, "failed")
    except Exception as e:
        log.warning(f"Event terminate: update_event_status failed: {e}")

    return {"message": f"事件 {seq} 终止请求已发送", "cancelled": cancelled}


@router.post("/{seq}/redispatch")
def redispatch_event(seq: int, local_only: bool = False, user: dict = Depends(require_no_session_token)):
    """手动重新派发事件任务（卡死 running>10min / failed 时人工收敛）。

    见 event-redispatch-design.md §4.1.2：
    - 关联任务非终态（dispatched/running）→ requeue 置回 queued；
    - 关联任务终态/缺失（含事件 failed）→ 复用原 task 快照参数重新 enqueue_task；
    - 事件状态回写 running。30s 内重复调用幂等拒绝。

    local_only=True（本机重试）：强制使用原 task.runtime_id 同机重跑，
    跳过 pick_engine，不换机器。原 runtime_id 不可用（下线/不健康）→ 返回 4xx 错误，不切换。
    """
    import json as _json
    from src.session_manager import db, db_lock
    from src.runtime.service import requeue_task, get_task, enqueue_task, update_task_status
    from src.api.event_store import update_event_status

    # 1) 幂等：30s 内已重派过则拒绝
    try:
        from src.user.db import get_meta_value, set_meta_value
        _last = get_meta_value(f"redispatch:{seq}") or ""
        if _last:
            import time as _time
            try:
                if _time.time() - float(_last) < 30:
                    raise HTTPException(status_code=409, detail={"code": 409, "message": "已重新派发过，请 30s 后再试"})
            except ValueError:
                pass
    except HTTPException:
        raise
    except Exception:
        pass

    evt = _load_event(seq)

    # 1.0）归属校验：仅仓库归属人（managed_repos）可重派
    from src.api.deps import check_repo_access
    check_repo_access(user, evt.get("repo_key", "") or "")

    # 1.5）onboarding 主事件重派 → 任务级自愈（Issue #46 重构后兼容）：
    #     历史 onboarding_exp/onboarding_explore 事件仍可能被 redispatch；onboarding 后端已改为「按模板创建标准任务」，
    #     旧事件映射到新任务 type（onboarding_exp→experience，onboarding_explore→dev）后走 retry_onboarding_task 补建。
    _onb_target = (
        "experience" if evt.get("event_type") == "onboarding_exp"
        else "dev" if evt.get("event_type") == "onboarding_explore" else ""
    )
    if _onb_target:
        from src.repo_key import split_owner_repo
        from src.api.onboarding import retry_onboarding_task
        _owner, _repo = split_owner_repo(evt.get("repo_key", "") or "")
        log.info(f"[redispatch] seq={seq} onboarding {_onb_target}: task-level retry, repo={evt.get('repo_key')}")
        _res = retry_onboarding_task(evt.get("repo_key", "") or "", _owner, _onb_target)
        # 原 seq 标记 failed（保持历史记录），新 seq 接管
        try:
            update_event_status(seq, "failed")
        except Exception:
            pass
        try:
            from src.user.db import set_meta_value as _smv
            import time as _time
            _smv(f"redispatch:{seq}", str(_time.time()))
        except Exception:
            pass
        if not (_res or {}).get("ok"):
            raise HTTPException(status_code=409, detail={"code": 409,
                "message": f"引导重派失败：{(_res or {}).get('reason', 'unknown')}"})
        return {"status": "redispatched", "task_id": "(new)", "event_seq": (_res or {}).get("seq", 0),
                "original_seq": seq}

    # 2) 查关联任务
    task_id = ""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, status, runtime_id FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1", (seq,))
        tr = c.fetchone()
        if tr:
            task_id, task_status, _orig_rid = tr[0], tr[1] or "", tr[2] or 0

    # 2.5) local_only：本机重试分支 — 强制使用原 runtime_id，跳过 pick_engine，不换机器
    if local_only:
        if not task_id:
            raise HTTPException(status_code=409, detail={"code": 409,
                "message": "无关联任务，无法本机重试"})
        old = get_task(task_id)
        if not old:
            raise HTTPException(status_code=404, detail={"code": 404,
                "message": "关联任务不存在，无法本机重试"})
        _orig_rid = old.get("runtime_id") or 0
        if not _orig_rid:
            raise HTTPException(status_code=409, detail={"code": 409,
                "message": "原任务未绑定引擎（runtime_id=0），无法本机重试"})
        # 健康校验：原 runtime_id 必须 online + healthy
        from src.runtime.service import runtime_credential_id, _remote_runtime_ready
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT status, healthy FROM agent_runtimes WHERE id=?", (_orig_rid,))
                _rh = c.fetchone()
        except Exception:
            _rh = None
        if not _rh or (_rh[0] or "") != "online" or not _rh[1]:
            raise HTTPException(status_code=409, detail={"code": 409,
                "message": "原执行机器不可用（引擎下线或不健康），无法本机重试"})
        # 远端引擎额外心跳校验
        _cid = runtime_credential_id(_orig_rid) or 0
        if _cid > 0 and not _remote_runtime_ready(_orig_rid):
            raise HTTPException(status_code=409, detail={"code": 409,
                "message": "原执行机器不可用（远端引擎失联），无法本机重试"})

        # 校验通过 → 复用原 task 快照参数 + 原 runtime_id 新建任务（不调 pick_engine）
        from src.api.event_pipeline import is_plugin_mode as _is_plugin_mode
        _sid = old.get("issue_session_id") or ""
        try:
            from src.api.event_pipeline import get_agent_workdir
            _cwd = get_agent_workdir(old.get("repo_key") or "", old.get("agent_type") or "",
                                    old.get("issue_id") or "")
        except Exception:
            _cwd = old.get("cwd") or ""
        _is_local_direct = (_cid == 0)
        _new_task_id = enqueue_task(
            agent_session_id=old.get("agent_session_id") or "",
            runtime_id=_orig_rid,
            event_seq=old.get("event_seq"),
            issue_id=old.get("issue_id") or "#0",
            repo_key=old.get("repo_key") or "",
            agent_type=old.get("agent_type") or "",
            skill=old.get("skill") or "",
            skill_id=old.get("skill_id") or 0,
            prompt=old.get("prompt") or "",
            cwd=_cwd,
            runtime_session_id=old.get("runtime_session_id") or "",
            event_type=old.get("event_type") or "",
            account=old.get("account") or "",
            user_id=old.get("user_id") or 0,
            repo_gitcode_token=old.get("repo_gitcode_token") or "",
            env_extra={},
            server_url=old.get("server_url") or "",
            issue_session_id=_sid,
            pool_tag=old.get("agent_type") or "" if _is_plugin_mode() else "",
        )
        # 本机 direct 引擎 → 触发直接执行
        if _is_local_direct and _new_task_id:
            try:
                from src.api.event_pipeline import _execute_agent, start_direct_agent
                from src.runtime.engine_select import engine_name as _en
                from src.repo_key import from_repo_key as _frk
                _be = _en(_orig_rid) or ""
                _skill_name = old.get("skill") or ""
                _rk = old.get("repo_key") or ""
                _evt = {
                    "repo_key": _rk,
                    "repository": _frk(_rk),
                    "source": (_rk.split(":", 1)[0] if ":" in _rk else "gitcode"),
                    "issue_id": old.get("issue_id") or "",
                    "agent_type": old.get("agent_type") or "",
                    "event_type": old.get("event_type") or "",
                    "type": old.get("event_type") or "",
                    "payload": {},
                }
                start_direct_agent(_execute_agent, (
                    old.get("repo_key") or "", seq, old.get("agent_type") or "",
                    _be, _skill_name, _evt, _orig_rid, _new_task_id,
                ), name=f"redispatch-{seq}")
                log.info(f"[redispatch] seq={seq} local_only: direct execution started on rid={_orig_rid}")
            except Exception as e:
                log.warning(f"[redispatch] seq={seq} local_only: direct execution failed: {e}")
        # 事件状态回写 running
        update_event_status(seq, "running")
        try:
            from src.user.db import set_meta_value
            import time as _time
            set_meta_value(f"redispatch:{seq}", str(_time.time()))
        except Exception:
            pass
        log.info(f"[redispatch] seq={seq} local_only: same-machine retry on rid={_orig_rid} task={_new_task_id} by={user.get('username','')}")
        return {"status": "redispatched", "task_id": _new_task_id or "(new)", "event_seq": seq, "local_only": True}

    # 3) 分支处理
    if task_id and task_status in ("dispatched", "running"):
        # 非终态 → requeue + reassign engine in one atomic operation
        try:
            from src.api.event_pipeline import is_plugin_mode
            _is_plugin = is_plugin_mode()
        except Exception:
            _is_plugin = False

        if _is_plugin:
            # Plugin mode: requeue + reassign to an engine with the right tag atomically
            _new_rid = 0
            with db_lock:
                c2 = db.cursor()
                # Get pool_tag + pool_engine_ids + repo_key + user_id for scope filtering
                c2.execute("SELECT pool_tag, pool_engine_ids, repo_key, user_id FROM agent_tasks WHERE id=?", (task_id,))
                _ptag_row = c2.fetchone()
                _ptag = ""
                _pool_eids = []
                _task_repo = ""
                _task_uid = 0
                if _ptag_row:
                    _ptag = _ptag_row[0] or ""
                    import json as _json_pe
                    try:
                        _raw_eids = _ptag_row[1] or ""
                        if _raw_eids:
                            _pool_eids = [int(x) for x in _json_pe.loads(_raw_eids) if x]
                    except Exception:
                        pass
                    _task_repo = _ptag_row[2] or ""
                    _task_uid = _ptag_row[3] or 0
                # Requeue
                c2.execute("UPDATE agent_tasks SET status='queued', started_at=NULL WHERE id=? AND status IN ('dispatched','running')", (task_id,))
                _requeued = c2.rowcount > 0
                if _requeued:
                    # Release busy slot on old runtime
                    c2.execute("SELECT runtime_id FROM agent_tasks WHERE id=?", (task_id,))
                    _old_rid_row = c2.fetchone()
                    if _old_rid_row and _old_rid_row[0]:
                        c2.execute("UPDATE agent_runtimes SET busy_slots = CASE WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END WHERE id=?", (_old_rid_row[0],))
                    # Reassign to an online engine with the right tag + scope isolation
                    # Prefer remote (credential_id > 0) so workers can claim; fall back to local direct
                    # pool_engine_ids 非空 → 限定场景绑定的引擎列表（与首次派发一致）
                    # pool_engine_ids 为空 → 加 scope 过滤（与 claim_by_tag 对齐）
                    if _ptag:
                        if _pool_eids:
                            _ph = ",".join("?" * len(_pool_eids))
                            _scope_extra = f" AND ar.id IN ({_ph})"
                            _scope_params = list(_pool_eids)
                        else:
                            _repo_bare = _task_repo.split(":", 1)[1] if ":" in _task_repo else _task_repo
                            _scope_extra = (
                                " AND (ar.credential_id = 0"
                                " OR rc.scope = 'global'"
                                " OR (rc.scope = 'repo' AND (rc.owner_id = ? OR rc.owner_id = ?))"
                                " OR (rc.scope = 'user' AND rc.owner_name ="
                                " (SELECT username FROM user_accounts WHERE id=?)))"
                            )
                            _scope_params = [_repo_bare, f"gitcode:{_repo_bare}", _task_uid]
                        # Query 1: prefer remote (credential_id > 0)
                        c2.execute(
                            "SELECT ar.id, ar.credential_id FROM agent_runtimes ar "
                            "JOIN engine_capabilities ec ON ec.runtime_id=ar.id AND ec.enabled=1 "
                            "AND (ec.tag=? OR ec.tag='all') "
                            "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
                            "WHERE ar.status='online' AND ar.healthy=1 AND ar.credential_id > 0"
                            + _scope_extra +
                            " ORDER BY ar.busy_slots ASC LIMIT 1",
                            (_ptag, *_scope_params)
                        )
                        _new_rid_row = c2.fetchone()
                        if not _new_rid_row:
                            # No remote engine with this tag, try local direct
                            c2.execute(
                                "SELECT ar.id, ar.credential_id FROM agent_runtimes ar "
                                "JOIN engine_capabilities ec ON ec.runtime_id=ar.id AND ec.enabled=1 "
                                "AND (ec.tag=? OR ec.tag='all') "
                                "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
                                "WHERE ar.status='online' AND ar.healthy=1"
                                + _scope_extra +
                                " ORDER BY ar.busy_slots ASC LIMIT 1",
                                (_ptag, *_scope_params)
                            )
                            _new_rid_row = c2.fetchone()
                        if _new_rid_row:
                            _new_rid = _new_rid_row[0]
                            c2.execute("UPDATE agent_tasks SET runtime_id=? WHERE id=?", (_new_rid, task_id))
                            log.info(f"[redispatch] seq={seq} plugin: reassigned task {task_id} to rid={_new_rid} (tag={_ptag})")
                        else:
                            c2.execute("UPDATE agent_tasks SET runtime_id=0 WHERE id=?", (task_id,))
                            log.warning(f"[redispatch] seq={seq} plugin: no engine with tag={_ptag}, cleared runtime_id")
                db.commit()

            # If reassigned to a local direct engine, trigger direct execution
            if _new_rid:
                try:
                    from src.runtime.service import runtime_credential_id
                    _cid = runtime_credential_id(_new_rid) or 0
                    if _cid == 0:
                        from src.api.event_pipeline import _execute_agent, start_direct_agent
                        from src.runtime.engine_select import engine_name
                        from src.repo_key import from_repo_key as _frk2
                        _be = engine_name(_new_rid) or ""
                        # Build a minimal evt for _execute_agent
                        _evt = {
                            "repo_key": evt.get("repo_key", ""),
                            "repository": _frk2(evt.get("repo_key", "")),
                            "source": (evt.get("repo_key", "").split(":", 1)[0] if ":" in evt.get("repo_key", "") else "gitcode"),
                            "issue_id": evt.get("issue_id", ""),
                            "agent_type": evt.get("agent_type", ""),
                            "event_type": evt.get("event_type", ""),
                            "type": evt.get("event_type", ""),
                            "payload": {},
                        }
                        start_direct_agent(_execute_agent, (
                            evt.get("repo_key", ""), seq, evt.get("agent_type", ""),
                            _be, "", _evt, _new_rid, task_id,
                        ), name=f"redispatch-{seq}")
                        log.info(f"[redispatch] seq={seq} plugin: direct execution started on rid={_new_rid}")
                except Exception as e:
                    log.warning(f"[redispatch] seq={seq} direct execution trigger failed: {e}")
        else:
            requeue_task(task_id)

        # Plugin mode: emit event
        try:
            from src.api.event_pipeline import is_plugin_mode
            if is_plugin_mode():
                import asyncio as _aio
                from src.api.event_bus import get_event_bus, GrapeEvent, EventDomain
                from src.api.pipeline_config import get_node_by_tag
                scene_id = ""
                node = get_node_by_tag(scene_id, evt["agent_type"] or "") if scene_id else None
                _bus = get_event_bus()
                _redispatch_event = GrapeEvent(
                    domain=EventDomain.PIPELINE,
                    type="pipeline/node/requeued",
                    repo_key=evt["repo_key"],
                    issue_id=evt["issue_id"],
                    agent_type=evt["agent_type"],
                    seq=seq,
                    payload={"task_id": task_id, "tag": evt["agent_type"] or "",
                             "scene_id": scene_id, "node_id": node["id"] if node else evt["agent_type"]},
                )
                loop = _aio.get_event_loop()
                if loop.is_running():
                    _aio.ensure_future(_bus.emit(_redispatch_event))
                else:
                    _aio.run(_bus.emit(_redispatch_event))
                log.info(f"[redispatch] seq={seq} plugin: pipeline/node/requeued emitted, task={task_id}, tag={evt['agent_type']}")
        except Exception as e:
            log.warning(f"[redispatch] plugin event emit failed: {e}")
    else:
        # 终态/缺失 → 复用原 task 快照参数新建任务（新 task_id，清空 runtime_session_id）
        from src.api.event_pipeline import is_plugin_mode
        if task_id:
            old = get_task(task_id)
            # If prompt is empty (from _create_direct_task), find the last task with a prompt
            if old and not (old.get("prompt") or "").strip():
                try:
                    with db_lock:
                        c4 = db.cursor()
                        c4.execute(
                            "SELECT id FROM agent_tasks WHERE event_seq=? AND prompt != '' AND prompt IS NOT NULL "
                            "ORDER BY created_at DESC LIMIT 1",
                            (seq,)
                        )
                        _good_row = c4.fetchone()
                        if _good_row:
                            old = get_task(_good_row[0])
                            log.info(f"[redispatch] seq={seq} recovered prompt from task {_good_row[0]}")
                except Exception:
                    pass
            if old:
                _sid = old.get("issue_session_id") or ""
                _env = {}
                # 重派时重新解析路由：旧 task 快照的 runtime_id 可能为 0（120s 超时回收产物）。
                # fallback 链：task.runtime_id → session.runtime_id → 拒派。
                _rid = old.get("runtime_id") or 0
                if not _rid:
                    # task runtime_id=0 → 查 session 主引擎
                    try:
                        with db_lock:
                            c3 = db.cursor()
                            c3.execute("SELECT runtime_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                                       (old.get("repo_key") or "", old.get("agent_type") or ""))
                            _sr = c3.fetchone()
                            if _sr and _sr[0]:
                                _rid = _sr[0]
                    except Exception:
                        pass
                try:
                    from src.runtime.engine_select import pick_engine
                    _rid = pick_engine(old.get("repo_key") or "", old.get("agent_type") or "", fallback_runtime_id=_rid) or 0
                except Exception:
                    _rid = 0
                # Plugin mode: prefer an engine that has the matching tag + scope isolation
                # pool_engine_ids 非空 → 限定场景绑定的引擎列表（与首次派发一致）
                # pool_engine_ids 为空 → 加 scope 过滤（与 claim_by_tag 对齐）
                if is_plugin_mode() and old.get("pool_tag"):
                    try:
                        _old_pool_eids = []
                        import json as _json_pe2
                        try:
                            _raw = old.get("pool_engine_ids") or ""
                            if _raw:
                                _old_pool_eids = [int(x) for x in _json_pe2.loads(_raw) if x]
                        except Exception:
                            pass
                        _old_repo = old.get("repo_key") or ""
                        _old_uid = old.get("user_id") or 0
                        if _old_pool_eids:
                            _ph2 = ",".join("?" * len(_old_pool_eids))
                            _scope_extra2 = f" AND ar.id IN ({_ph2})"
                            _scope_params2 = list(_old_pool_eids)
                        else:
                            _repo_bare2 = _old_repo.split(":", 1)[1] if ":" in _old_repo else _old_repo
                            _scope_extra2 = (
                                " AND (ar.credential_id = 0"
                                " OR rc.scope = 'global'"
                                " OR (rc.scope = 'repo' AND (rc.owner_id = ? OR rc.owner_id = ?))"
                                " OR (rc.scope = 'user' AND rc.owner_name ="
                                " (SELECT username FROM user_accounts WHERE id=?)))"
                            )
                            _scope_params2 = [_repo_bare2, f"gitcode:{_repo_bare2}", _old_uid]
                        with db_lock:
                            c3 = db.cursor()
                            c3.execute(
                                "SELECT ar.id FROM agent_runtimes ar "
                                "JOIN engine_capabilities ec ON ec.runtime_id=ar.id AND ec.enabled=1 "
                                "AND (ec.tag=? OR ec.tag='all') "
                                "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
                                "WHERE ar.status='online' AND ar.healthy=1"
                                + _scope_extra2 +
                                " ORDER BY ar.busy_slots ASC LIMIT 1",
                                (old.get("pool_tag"), *_scope_params2)
                            )
                            _tagged_rid = c3.fetchone()
                            if _tagged_rid:
                                _rid = _tagged_rid[0]
                    except Exception:
                        pass
                if not _rid and not is_plugin_mode():
                    raise HTTPException(status_code=409, detail={"code": 409, "message": "无可用引擎，无法重派"})
                # Determine engine type to choose execution path
                # Check credential_id directly (not just runtime_execution_kind, which is affected by global mode)
                try:
                    from src.runtime.service import runtime_credential_id
                    _cid = runtime_credential_id(_rid) or 0
                except Exception:
                    _cid = -1
                _is_local_direct = (_cid == 0)
                # cwd
                try:
                    from src.api.event_pipeline import get_agent_workdir
                    _cwd = get_agent_workdir(old.get("repo_key") or "", old.get("agent_type") or "",
                                            old.get("issue_id") or "")
                except Exception:
                    _cwd = old.get("cwd") or ""

                # Always enqueue (stores full prompt + env), then trigger direct if local
                _new_task_id = enqueue_task(
                    agent_session_id=old.get("agent_session_id") or "",
                    runtime_id=_rid,
                    event_seq=old.get("event_seq"),
                    issue_id=old.get("issue_id") or "#0",
                    repo_key=old.get("repo_key") or "",
                    agent_type=old.get("agent_type") or "",
                    skill=old.get("skill") or "",
                    skill_id=old.get("skill_id") or 0,
                    prompt=old.get("prompt") or "",
                    cwd=_cwd,
                    runtime_session_id=old.get("runtime_session_id") or "",
                    event_type=old.get("event_type") or "",
                    account=old.get("account") or "",
                    user_id=old.get("user_id") or 0,
                    repo_gitcode_token=old.get("repo_gitcode_token") or "",
                    env_extra={},
                    server_url=old.get("server_url") or "",
                    issue_session_id=_sid,
                    pool_tag=old.get("agent_type") or "" if is_plugin_mode() else "",
                    pool_agent_type=old.get("agent_type") or "" if is_plugin_mode() else "",
                )
                # If local direct engine, trigger direct execution (no worker will claim)
                if _is_local_direct and _new_task_id:
                    try:
                        from src.api.event_pipeline import _execute_agent, start_direct_agent
                        from src.runtime.engine_select import engine_name as _en
                        from src.repo_key import from_repo_key as _frk
                        _be = _en(_rid) or ""
                        _skill_name = old.get("skill") or ""
                        _rk = old.get("repo_key") or ""
                        _evt = {
                            "repo_key": _rk,
                            "repository": _frk(_rk),
                            "source": (_rk.split(":", 1)[0] if ":" in _rk else "gitcode"),
                            "issue_id": old.get("issue_id") or "",
                            "agent_type": old.get("agent_type") or "",
                            "event_type": old.get("event_type") or "",
                            "type": old.get("event_type") or "",
                            "payload": {},
                        }
                        start_direct_agent(_execute_agent, (
                            old.get("repo_key") or "", seq, old.get("agent_type") or "",
                            _be, _skill_name, _evt, _rid, _new_task_id,
                        ), name=f"redispatch-{seq}")
                        log.info(f"[redispatch] seq={seq} direct execution started on rid={_rid}")
                    except Exception as e:
                        log.warning(f"[redispatch] seq={seq} direct execution failed: {e}")
            else:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "关联任务不存在，无法重派"})
        else:
            # 无关联任务（引擎不可用时未创建 task 快照的历史事件）
            _event_type = evt.get("event_type", "unknown")

            # A2A 事件不能走 run_pipeline（payload 格式不同，且无 webhook header 映射）
            # → 直接重新触发 A2A 交办
            if _event_type == "a2a_event":
                try:
                    _payload = evt.get("payload") or evt.get("payload_raw") or "{}"
                    if isinstance(_payload, str):
                        _payload = _json.loads(_payload)
                    _repo_key = evt.get("repo_key", "") or ""
                    _bot_token = ""
                    try:
                        from src.user.db import get_repo_bot_token_no_global
                        _bot_token = get_repo_bot_token_no_global(_repo_key) or ""
                    except Exception:
                        pass
                    import urllib.request as _url_req
                    _a2a_body = _json.dumps({"agent": evt.get("agent_type", ""), "payload": _payload}).encode()
                    _a2a_req = _url_req.Request(
                        f"http://localhost:3000/api/a2a",
                        data=_a2a_body,
                        headers={"Content-Type": "application/json", "X-Bot-Token": _bot_token},
                        method="POST"
                    )
                    with _url_req.urlopen(_a2a_req, timeout=30) as _a2a_resp:
                        _a2a_result = _json.loads(_a2a_resp.read().decode())
                    if _a2a_result.get("status") == "ok":
                        _new_seq = _a2a_result.get("seq", 0)
                        log.info(f"[redispatch] seq={seq} a2a re-dispatch ok, new seq={_new_seq}")
                        update_event_status(seq, "failed")
                        try:
                            from src.user.db import set_meta_value as _smv
                            import time as _tm
                            _smv(f"redispatch:{seq}", str(_tm.time()))
                        except Exception:
                            pass
                        return {"status": "redispatched", "task_id": "(new)", "event_seq": _new_seq,
                                "original_seq": seq}
                    else:
                        raise HTTPException(status_code=409, detail={"code": 409,
                            "message": f"重派失败：A2A 重新交办未成功（{_a2a_result.get('status', 'unknown')}）"})
                except HTTPException:
                    raise
                except Exception as e:
                    log.warning(f"[redispatch] seq={seq} a2a re-dispatch failed: {e}")
                    raise HTTPException(status_code=500, detail={"code": 500, "message": f"重派异常: {str(e)[:100]}"})

            # 非 A2A 事件：从 events 表恢复参数重走 run_pipeline
            # 构建最小 webhook evt，复用 run_pipeline 路由+引擎选择+入队/执行
            try:
                from src.api.event_pipeline import run_pipeline as _rp
                _payload = evt.get("payload_raw") or evt.get("payload") or "{}"
                if isinstance(_payload, str):
                    try:
                        _payload = _json.loads(_payload)
                    except Exception:
                        _payload = {}
                _event_type = evt.get("event_type", "unknown")
                # 构造 normalize_event 格式的 evt dict
                _fake_raw = _json.dumps(_payload).encode() if isinstance(_payload, dict) else b"{}"
                # 反推 event_header：issue_opened → Issue Hook, pull_request_merged → Merge Request Hook
                _header_map = {"issue_opened": "Issue Hook", "issue_updated": "Issue Hook",
                               "issue_comment": "Note Hook", "pull_request_merged": "Merge Request Hook",
                               "pull_request_opened": "Merge Request Hook", "push": "push"}
                _header = _header_map.get(_event_type, _event_type)
                _rp_result = _rp(_fake_raw, {"X-GitCode-Event": _header},
                                 account=evt.get("account", ""), repo_key_override=evt.get("repo_key", ""))
                if _rp_result.get("ok"):
                    log.info(f"[redispatch] seq={seq} re-run_pipeline ok, new seq={_rp_result.get('seq')}")
                    # 原 seq 事件标记 failed（保持历史记录），新 seq 接管执行
                    update_event_status(seq, "failed")
                    try:
                        from src.user.db import set_meta_value as _smv
                        import time as _tm
                        _smv(f"redispatch:{seq}", str(_tm.time()))
                    except Exception:
                        pass
                    return {"status": "redispatched", "task_id": "(new)", "event_seq": _rp_result.get("seq", 0),
                            "original_seq": seq}
                else:
                    raise HTTPException(status_code=409, detail={"code": 409,
                        "message": f"重派失败：run_pipeline 未成功（{_rp_result.get('event_type', 'unknown')}）"})
            except HTTPException:
                raise
            except Exception as e:
                log.warning(f"[redispatch] seq={seq} re-run_pipeline failed: {e}")
                raise HTTPException(status_code=500, detail={"code": 500, "message": f"重派异常: {str(e)[:100]}"})

    # 4) 事件状态回写 running
    update_event_status(seq, "running")

    # 5) 记录幂等时间戳
    try:
        from src.user.db import set_meta_value
        import time as _time
        set_meta_value(f"redispatch:{seq}", str(_time.time()))
    except Exception:
        pass

    log.info(f"[redispatch] seq={seq} repo={evt['repo_key']} agent={evt['agent_type']} task={task_id or '(new)'} by={user.get('username','')}")
    return {"status": "redispatched", "task_id": task_id or "(new)", "event_seq": seq}


# ====== Event 对话（event-conversation-design.md）======

class ConversationMessage(BaseModel):
    message: str


class KnowledgeBody(BaseModel):
    scope: str = "issue"           # project / issue / issue_agent
    content: str = ""
    status: str = "accepted"       # accepted / rejected


class FlowResetBody(BaseModel):
    to_stage: str = "vod"


def _load_event(seq: int) -> dict:
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, repo_key, issue_id, agent_type, event_type, title, result, status, payload, source, actor FROM events WHERE seq=?",
                (seq,))
            row = c.fetchone()
        except Exception:
            row = None
    if not row:
        # 返回当前最大 seq 供前端回退
        try:
            c.execute("SELECT MAX(seq) FROM events")
            max_seq = c.fetchone()[0] or 0
        except Exception:
            max_seq = 0
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件未找到", "max_seq": max_seq})
    return {"seq": row[0], "repo_key": row[1], "issue_id": row[2] or "", "agent_type": row[3] or "",
            "event_type": row[4] or "", "title": row[5] or "", "result": row[6] or "", "status": row[7] or "",
            "payload": row[8] or "", "source": row[9] or "gitcode", "actor": row[10] or "",
            "account": row[1].replace("gitcode:", "") if row[1] else ""}


def _thread_gate(repo_key: str, issue_id: str) -> dict:
    """门禁：Issue pipeline 结束（stage='done'）→ 关闭评论。无记录默认开放。"""
    if not issue_id:
        return {"open": True, "reason": ""}
    from src.api.session_store import get_issue_stage
    st = get_issue_stage(repo_key, issue_id) or {}
    if (st.get("stage") or "vod") == "done":
        return {"open": False, "reason": "该 Issue 流程已结束，评论已关闭"}
    return {"open": True, "reason": ""}


def _add_thread_message(anchor_seq: int, repo_key: str, issue_id: str, agent_type: str,
                        role: str, content: str, created_by: str = "",
                        task_id: str = "", generate_status: int = 0):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        if task_id:
            c.execute(
                "INSERT INTO event_thread (anchor_seq, repo_key, issue_id, agent_type, "
                "role, content, created_by, task_id, generate_status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (anchor_seq, repo_key, issue_id, agent_type, role, content, created_by,
                 task_id, generate_status))
        else:
            c.execute(
                "INSERT INTO event_thread (anchor_seq, repo_key, issue_id, agent_type, "
                "role, content, created_by) VALUES (?,?,?,?,?,?,?)",
                (anchor_seq, repo_key, issue_id, agent_type, role, content, created_by))
        db.commit()
        mid = c.lastrowid
    return mid


def _list_thread(anchor_seq: int, limit: int = 50) -> list:
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT id, role, content, created_by, created_at FROM event_thread WHERE anchor_seq=? ORDER BY id ASC LIMIT ?",
                      (anchor_seq, limit))
            rows = c.fetchall()
        except Exception:
            rows = []
    return [{"id": r[0], "role": r[1], "content": r[2], "created_by": r[3] or "", "created_at": normalize_event_time(r[4])} for r in rows]


def _require_event_bot_token(request: Request, repo_key: str):
    """agent 端点鉴权：bot token 或会话级临时 token（复作用域）二选一通过。

    与 a2a_endpoint 同款（仓库级鉴权）；会话 token 校验 repo 作用域匹配（M2）。
    """
    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not token:
        token = request.headers.get("X-Bot-Token", "") or ""
    from src.user.db import get_repo_bot_token_no_global
    from src.api.auth_token import verify_session_token_for_repo
    expected = get_repo_bot_token_no_global(repo_key) or ""
    if expected and token == expected:
        return
    if verify_session_token_for_repo(token, repo_key):
        return
    raise HTTPException(status_code=403, detail={"code": 403, "message": "invalid token"})


def _cancel_issue_tasks(repo_key: str, issue_id: str) -> int:
    from src.session_manager import db, db_lock
    from src.runtime.service import cancel_task
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running')",
                  (repo_key, issue_id))
        ids = [r[0] for r in c.fetchall()]
    cancelled = 0
    for tid in ids:
        try:
            cancel_task(tid)
            cancelled += 1
        except Exception:
            pass
    return cancelled


# Fix 2：直接执行的无输出超时（每次收到事件重置计时；超过则判定疑似挂起）
_CONV_NO_OUTPUT_TIMEOUT = float(os.environ.get("CONVERSATION_NO_OUTPUT_TIMEOUT", "300"))


class _ConvStuck(Exception):
    """对话执行无输出超时（疑似挂起）。"""


async def _iter_with_timeout(aiter, timeout: float):
    """逐事件等待；无事件到达超时 → 抛 _ConvStuck（每次事件重置计时）。"""
    while True:
        try:
            item = await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
        except asyncio.TimeoutError:
            raise _ConvStuck() from None
        if item is None:
            return
        yield item


async def _direct_attempt(prompt, ws, session_id, repository, bot_token, anchor_seq, repo_key, agent_type, backend_name="hermes"):
    """直接模式单次执行：yield (text/thinking) 事件；以 sentinel 结束（_stuck / _done）。"""
    from src.runtime.executor import consume_stream_async
    acc = ""
    new_oc = ""
    gen = consume_stream_async(
        prompt, ws, session_id=session_id, event_type="event_conversation",
        backend_name=backend_name, account=repository, bot_token=bot_token,
        agent_name="Event Dialogue Agent",
        agent_label=f"[event-conv|repo:{repo_key}|seq:{anchor_seq}]",
    )
    try:
        async for evt_type, data in _iter_with_timeout(gen, _CONV_NO_OUTPUT_TIMEOUT):
            if evt_type == "text":
                t = data.get("text") if isinstance(data, dict) else str(data)
                if t:
                    acc += t
                yield ("text", t)
            elif evt_type == "reasoning":
                yield ("thinking", data.get("text") if isinstance(data, dict) else str(data))
            elif evt_type == "done" and isinstance(data, dict):
                acc = data.get("text", "") or acc
                new_oc = data.get("oc_session_id", "") or new_oc
                break
    except _ConvStuck:
        yield ("_stuck", "")
        return
    yield ("_done", {"final_text": acc, "new_oc": new_oc})


async def _run_conversation(evt: dict, user_message: str, username: str, conv_task_id: str = ""):
    """门禁已通过：写用户消息 → 复用 issue 会话运行 agent → 回写 agent 回复。

    生成器事件：(thinking, text) / (text, 增量) / (done, {reply, messages})。
    - direct：SSE 流式（consume_stream_async）
    - 队列模式：入队 + 轮询（knowledge_files 由 enqueue_task 打包，见 runtime/service.py）
    """
    from src.config import WORKSPACE_DIR
    from src.repo_key import from_repo_key
    from src.api.knowledge import _issue_num_from
    repo_key, agent_type, issue_id, anchor_seq = evt["repo_key"], evt["agent_type"], evt["issue_id"], evt["seq"]

    _add_thread_message(anchor_seq, repo_key, issue_id, agent_type, "user", user_message, username,
                        task_id=conv_task_id, generate_status=1 if conv_task_id else None)
    yield ("thinking", "正在连接 agent...")

    # 会话复用：issue 级会话（无 issue 回退 repo 级），见 event-conversation-design.md §3.2.1
    issue_num = _issue_num_from(issue_id)
    issue_sess_id = ""
    oc_session_id = ""
    if issue_num:
        from src.api.session_store import get_or_create_issue_session
        isess = get_or_create_issue_session(repo_key, agent_type, f"#{issue_num}")
        issue_sess_id = isess.get("id") or ""
        oc_session_id = isess.get("runtime_session_id") or ""
    if not oc_session_id:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_session_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                      (repo_key, agent_type))
            row = c.fetchone()
        oc_session_id = row[0] or "" if row else ""

    from src.prompt_builder import build_conversation_prompt
    prompt, _inj_ids = build_conversation_prompt(evt, user_message, _list_thread(anchor_seq), agent_type, issue_num)

    from src.repo_key import parse_repo_key, from_repo_key
    _provider, _, _ = parse_repo_key(repo_key)
    if _provider == "github":
        from src.user.db import get_github_token
        bot_token = get_github_token(repo_key=repo_key) or ""
    else:
        from src.user.db import get_gitcode_token
        bot_token = get_gitcode_token(repo_key=repo_key) or ""
    repository = from_repo_key(repo_key)

    from src.runtime.service import get_execution_mode, EXEC_MODE_DIRECT, enqueue_task, wait_task_terminal, cancel_task, runtime_execution_kind
    final_text = ""
    new_oc = oc_session_id

    # 引擎选择：按 issue 会话的 runtime_id 判断（credential_id=0 强制 direct）
    _conv_agent_sid = ""
    _conv_rid = 0
    from src.session_manager import db as _conv_db, db_lock as _conv_db_lock
    with _conv_db_lock:
        c = _conv_db.cursor()
        c.execute("SELECT id, runtime_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                  (repo_key, agent_type))
        _conv_row = c.fetchone()
    if _conv_row:
        _conv_agent_sid = _conv_row[0] or ""
        _conv_rid = _conv_row[1] or 0
    _conv_kind = runtime_execution_kind(_conv_rid) if _conv_rid else EXEC_MODE_DIRECT

    if _conv_kind != EXEC_MODE_DIRECT:
        # 队列模式：入队 + 轮询（打字机效果降级，任务终态一次返回）
        # 技能随任务下发（Fix A）：event-dialogue 打包进 skill_files → worker 物化到 DEVOPS_SKILLS_DIR
        yield ("thinking", "任务已入队，等待 worker 执行...")
        runtime_id = _conv_rid
        agent_sid = _conv_agent_sid
        if agent_sid:
            from src.config import SKILLS_DIR, resolve_official_skill_path
            event_dialogue_path = resolve_official_skill_path("event-dialogue") \
                or os.path.join(SKILLS_DIR, "event-dialogue", "SKILL.md")
            from src.api.event_pipeline import get_agent_workdir
            # Fix 1：cwd 用该 issue 的 agent 目录（与 issue 会话项目一致）
            ws = get_agent_workdir(repo_key, agent_type, issue_id)
            os.makedirs(ws, exist_ok=True)
            # 路由对齐 _enqueue_pipeline_task：pick_engine 按引擎集合/会话主引擎选引擎
            # （专属→全局→本机；无则跳过，不再回落硬编码默认）
            try:
                from src.runtime.engine_select import pick_engine
                runtime_id = pick_engine(repo_key, agent_type, fallback_runtime_id=runtime_id or 0) or 0
            except Exception:
                runtime_id = 0
            if not runtime_id:
                from src.api.event_pipeline import is_plugin_mode
                if not is_plugin_mode():
                    final_text = "（对话执行失败：无可用引擎/会话）"
            else:
                tid = enqueue_task(
                    agent_session_id=agent_sid, runtime_id=runtime_id, prompt=prompt, cwd=ws,
                    runtime_session_id=oc_session_id,
                    issue_session_id=issue_sess_id,        # Fix E：串行槽绑定本 issue 会话
                    event_type="event_conversation",
                    repo_key=repo_key, agent_type=agent_type,
                    skill="event-dialogue",                 # Fix A：打包下发对话技能
                    skill_path=event_dialogue_path,
                    account=repository, repo_gitcode_token=bot_token,
                    issue_id=issue_id or "#0",
                    pool_agent_type=agent_type if not runtime_id else "",  # plugin 广播模式下需要 pool_tag
                )
                # Fix C：超时/失败要有回显与清理，不再 30 分钟静默
                task = None
                failed = False
                try:
                    task = await wait_task_terminal(tid, timeout=300.0)
                except TimeoutError:
                    failed = True
                    cancel_task(tid)
                    final_text = "（对话执行超时，请稍后重试）"
                except Exception as e:
                    failed = True
                    cancel_task(tid)
                    final_text = f"（对话执行失败：{e}）"
                # Fix 2：超时/失败后清空 issue 会话，避免下轮复用挂起会话
                if failed and issue_sess_id:
                    from src.api.session_store import update_issue_session
                    update_issue_session(issue_sess_id, runtime_session_id="")
                if task and not final_text:
                    final_text = task.get("result_text") or ""
                    try:
                        import json as _json
                        usage = _json.loads(task.get("usage_json") or "{}") or {}
                        new_oc = usage.get("session_id") or oc_session_id
                    except Exception:
                        pass
        else:
            final_text = "（对话执行失败：无可用 runtime/会话）"
    else:
        # direct：SSE 流式；cwd 用该 issue 的 agent 目录（Fix 1）；无输出超时则重置会话重试一次（Fix 2）
        from src.api.event_pipeline import get_agent_workdir
        ws = get_agent_workdir(repo_key, agent_type, issue_id)
        os.makedirs(ws, exist_ok=True)
        result = None
        async for kind, data in _direct_attempt(prompt, ws, oc_session_id, repository,
                                                bot_token, anchor_seq, repo_key, agent_type):
            if kind == "_stuck":
                yield ("thinking", "对话执行异常，正在重置会话重试...")
            elif kind == "_done":
                result = data
            else:
                yield (kind, data)
        if result is None and oc_session_id:
            # Fix 2：复用的 issue 会话疑似挂起 → 重置会话后以全新会话重试一次
            from src.api.session_store import update_issue_session
            if issue_sess_id:
                update_issue_session(issue_sess_id, runtime_session_id="")
            oc_session_id = ""
            async for kind, data in _direct_attempt(prompt, ws, "", repository,
                                                    bot_token, anchor_seq, repo_key, agent_type):
                if kind == "_done":
                    result = data
                elif kind != "_stuck":
                    yield (kind, data)
        if result:
            final_text = result.get("final_text") or ""
            new_oc = result.get("new_oc") or ""

    if new_oc and new_oc != oc_session_id and issue_sess_id:
        from src.api.session_store import update_issue_session
        update_issue_session(issue_sess_id, runtime_session_id=new_oc)

    if not final_text:
        final_text = "（agent 未返回回复，请稍后重试）"
    _add_thread_message(anchor_seq, repo_key, issue_id, agent_type, "agent", final_text, agent_type,
                        task_id=conv_task_id, generate_status=2 if conv_task_id else None)
    yield ("done", {"reply": final_text, "messages": _list_thread(anchor_seq)})


@router.get("/{seq}/thread")
def get_event_thread(seq: int, user: dict = Depends(require_no_session_token)):
    evt = _load_event(seq)
    from src.api.knowledge import read_knowledge, _issue_num_from
    issue_num = _issue_num_from(evt["issue_id"])
    return {
        "seq": seq,
        "repo_key": evt["repo_key"],
        "issue_id": evt["issue_id"],
        "agent_type": evt["agent_type"],
        "gate": _thread_gate(evt["repo_key"], evt["issue_id"]),
        "messages": _list_thread(seq),
        "knowledge": read_knowledge(evt["repo_key"], agent_type=evt["agent_type"], issue_num=issue_num),
    }


@router.delete("/{seq}/chat")
def delete_event_thread(seq: int, user: dict = Depends(require_no_session_token)):
    """Issue #100: 清理事件关联的对话消息（event_thread 记录）。"""
    evt = _load_event(seq)
    from src.api.routes.knowledge import _check_repo_member
    _check_repo_member(user, evt["repo_key"])
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM event_thread WHERE anchor_seq=?", (seq,))
        db.commit()
    return {"ok": True, "message": "对话已清理"}


@router.delete("/{seq}/knowledge/{scope}")
def delete_event_knowledge(seq: int, scope: str, user: dict = Depends(require_no_session_token)):
    """Issue #100: 删除事件关联的 agent 沉淀知识（文件 + DB + ref）。"""
    evt = _load_event(seq)
    if scope not in ("project", "issue", "issue_agent"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 project/issue/issue_agent"})
    from src.api.routes.knowledge import _check_repo_member
    _check_repo_member(user, evt["repo_key"])

    from src.api.knowledge import knowledge_path, knowledge_storage_key, _issue_num_from
    from src.session_manager import db, db_lock
    import os

    issue_num = _issue_num_from(evt["issue_id"])
    agent_type = evt["agent_type"]
    _ag = agent_type if scope == "issue_agent" else ""
    _in = issue_num if scope != "project" else 0

    # Issue #100: issue/issue_agent 级必须有 issue_id
    if scope != "project" and _in == 0:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "该事件无关联 Issue，无法删除 issue/issue_agent 级知识"})

    # 1. 删工作区文件
    file_path = knowledge_path(evt["repo_key"], scope, agent_type=_ag, issue_num=_in)
    if os.path.isfile(file_path):
        os.remove(file_path)

    # 2. 删 OBS 对象（如有）
    try:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            obs_key = knowledge_storage_key(evt["repo_key"], scope, agent_type=_ag, issue_num=_in)
            storage.delete_object(obs_key)
    except Exception:
        pass

    # 3. 删 DB 记录（agent_shared_knowledge + scene_knowledge_ref）
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id FROM agent_shared_knowledge WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=?",
            (evt["repo_key"], scope, _ag, _in)
        ).fetchall()
        for (kid,) in rows:
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id=?", (kid,))
        c.execute(
            "DELETE FROM agent_shared_knowledge WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=?",
            (evt["repo_key"], scope, _ag, _in)
        )
        db.commit()

    return {"ok": True, "scope": scope, "message": "知识已删除"}


@router.post("/{seq}/chat")
async def send_conversation(seq: int, body: ConversationMessage, user: dict = Depends(require_no_session_token)):
    evt = _load_event(seq)
    gate = _thread_gate(evt["repo_key"], evt["issue_id"])
    if not gate["open"]:
        raise HTTPException(status_code=409, detail={"code": 409, "message": gate["reason"] or "该 Issue 流程已结束，评论已关闭"})
    if not (body.message or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "消息不能为空"})
    final = {"reply": "", "messages": []}
    async for evt_type, data in _run_conversation(evt, body.message.strip(), user.get("username") or ""):
        if evt_type == "done":
            final = data
    return final


@router.post("/{seq}/chat/stream")
async def stream_conversation(seq: int, body: ConversationMessage, user: dict = Depends(require_no_session_token)):
    """SSE 流式对话（direct 模式；队列模式为一次性 done）。"""
    from fastapi.responses import StreamingResponse
    evt = _load_event(seq)
    gate = _thread_gate(evt["repo_key"], evt["issue_id"])
    if not gate["open"]:
        raise HTTPException(status_code=409, detail={"code": 409, "message": gate["reason"] or "该 Issue 流程已结束，评论已关闭"})
    if not (body.message or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "消息不能为空"})
    username = user.get("username") or ""

    async def event_stream():
        import json as _json
        async for evt_type, data in _run_conversation(evt, body.message.strip(), username):
            if evt_type == "thinking":
                yield f"data: {_json.dumps({'type': 'thinking', 'text': data})}\n\n"
            elif evt_type == "text":
                yield f"data: {_json.dumps({'type': 'text', 'text': data})}\n\n"
            elif evt_type == "done":
                yield f"data: {_json.dumps({'type': 'done', 'reply': data['reply'], 'messages': data['messages']})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


# ===== 事件对话持久化查询（对齐 Chat Agent v31）=====
import threading as _conv_threading
import time as _conv_time
import uuid as _conv_uuid

_conv_tasks: dict = {}
_conv_tasks_lock = _conv_threading.Lock()

# 清理过期任务（>30 分钟）
def _cleanup_conv_tasks():
    with _conv_tasks_lock:
        now = _conv_time.time()
        expired = [tid for tid, t in _conv_tasks.items()
                   if t.get("finished_at") and now - t["finished_at"] > 1800]
        for tid in expired:
            _conv_tasks.pop(tid, None)


@router.post("/{seq}/chat/submit")
async def submit_conversation(seq: int, body: ConversationMessage, user: dict = Depends(require_no_session_token)):
    """提交对话查询 → 立即返回 task_id。后台异步执行 agent。"""
    _cleanup_conv_tasks()
    evt = _load_event(seq)
    gate = _thread_gate(evt["repo_key"], evt["issue_id"])
    if not gate["open"]:
        raise HTTPException(status_code=409, detail={"code": 409, "message": gate["reason"] or "该 Issue 流程已结束，评论已关闭"})
    if not (body.message or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "消息不能为空"})

    conv_task_id = "conv_" + _conv_uuid.uuid4().hex[:12]
    username = user.get("username") or ""

    # 写 user 消息（带 task_id + generate_status=1）
    _add_thread_message(seq, evt["repo_key"], evt["issue_id"], evt["agent_type"],
                        "user", body.message.strip(), username,
                        task_id=conv_task_id, generate_status=1)

    # 写入内存任务缓冲
    with _conv_tasks_lock:
        _conv_tasks[conv_task_id] = {
            "status": "running", "events": [], "result": "", "error": "",
            "seq": seq, "repo_key": evt["repo_key"], "agent_type": evt["agent_type"],
            "issue_id": evt["issue_id"], "username": username,
            "message": body.message.strip(),
            "created_at": _conv_time.time(), "finished_at": 0,
        }

    # 启动后台执行（run_in_executor）
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _execute_conv_task, conv_task_id, loop)

    return {"task_id": conv_task_id, "seq": seq, "status": "running"}


def _execute_conv_task(task_id: str, loop):
    """后台执行对话任务（线程池中运行，call_soon_threadsafe 投递事件到 event loop）。"""
    import json as _json
    _worker_final_text = ""
    injected_ids = []
    with _conv_tasks_lock:
        task = _conv_tasks.get(task_id)
        if not task:
            return
        task["status"] = "running"
        seq = task["seq"]
        repo_key = task["repo_key"]
        agent_type = task["agent_type"]
        issue_id = task["issue_id"]
        username = task["username"]
        message = task["message"]

    try:
        evt = _load_event(seq)
        # 直接用同步 stream_devops_agent（在线程中运行，不阻塞主 loop）
        from src.runtime.executor import stream_devops_agent
        _worker_final_text = ""
        repo_key = evt["repo_key"]
        agent_type = evt["agent_type"]
        issue_id = evt["issue_id"]
        anchor_seq = evt["seq"]
        from src.config import WORKSPACE_DIR
        from src.repo_key import from_repo_key
        from src.api.knowledge import _issue_num_from
        from src.api.event_pipeline import get_agent_workdir
        from src.prompt_builder import build_conversation_prompt
        from src.user.db import get_gitcode_token
        from src.api.session_store import get_or_create_issue_session
        issue_num = _issue_num_from(issue_id)
        # 会话复用
        issue_sess_id = ""
        oc_session_id = ""
        if issue_num:
            isess = get_or_create_issue_session(repo_key, agent_type, f"#{issue_num}")
            issue_sess_id = isess.get("id") or ""
            oc_session_id = isess.get("runtime_session_id") or ""
        if not oc_session_id:
            from src.session_manager import db as _sdb, db_lock as _sdbl
            with _sdbl:
                c = _sdb.cursor()
                c.execute("SELECT runtime_session_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_key, agent_type))
                row = c.fetchone()
            oc_session_id = row[0] or "" if row else ""
        ws = get_agent_workdir(repo_key, agent_type, issue_id)
        import os
        os.makedirs(ws, exist_ok=True)
        prompt, injected_ids = build_conversation_prompt(evt, message, _list_thread(anchor_seq), agent_type, issue_num)
        from src.repo_key import parse_repo_key
        _provider, _, _ = parse_repo_key(repo_key)
        if _provider == "github":
            from src.user.db import get_github_token
            bot_token = get_github_token(repo_key=repo_key) or ""
        else:
            from src.user.db import get_gitcode_token
            bot_token = get_gitcode_token(repo_key=repo_key) or ""
        repository = from_repo_key(repo_key)
        # 用 hermes 引擎（对齐 Chat Agent）
        for evt_type, data in stream_devops_agent(
            prompt, cwd=ws, session_id=oc_session_id, event_type="event_dialogue",
            backend_name="hermes", account=repository, bot_token=bot_token,
            agent_name="Event Dialogue Agent",
            agent_label=f"[event-conv|repo:{repo_key}|seq:{anchor_seq}]",
        ):
            if evt_type == "text" or evt_type == "token":
                t = data if isinstance(data, str) else str(data)
                _worker_final_text += t
                loop.call_soon_threadsafe(_push_conv_event, task_id, "text", t)
            elif evt_type == "done" or evt_type == "complete":
                if isinstance(data, dict):
                    _worker_final_text = data.get("text", "") or _worker_final_text
                elif data:
                    _worker_final_text = data if isinstance(data, str) else str(data)
                break
            elif evt_type == "error":
                loop.call_soon_threadsafe(_push_conv_event, task_id, "error", str(data)[:500])
                break
    except Exception as e:
        _worker_final_text = f"（对话执行失败：{e}）"
        loop.call_soon_threadsafe(_push_conv_event, task_id, "error", _worker_final_text)

    # finally 写 DB（带 task_id + generate_status）
    if _worker_final_text:
        _add_thread_message(seq, repo_key, issue_id, agent_type, "agent", _worker_final_text, agent_type,
                            task_id=task_id, generate_status=2)
    else:
        _add_thread_message(seq, repo_key, issue_id, agent_type, "agent", "（无返回内容）", agent_type,
                            task_id=task_id, generate_status=3)
        _worker_final_text = "（无返回内容）"

    # Issue #67: 记录注入的知识 ID 到事件线程
    if injected_ids:
        _add_thread_message(seq, repo_key, issue_id, agent_type, "system",
                            f"📌 本次注入知识：{', '.join(str(i) for i in injected_ids)}")

    with _conv_tasks_lock:
        t = _conv_tasks.get(task_id)
        if t:
            t["status"] = "done"
            t["result"] = _worker_final_text
            t["finished_at"] = _conv_time.time()
            t["events"].append({"type": "done", "data": _worker_final_text})
    loop.call_soon_threadsafe(_push_conv_event, task_id, "done", _worker_final_text)


async def _collect_conv_events(gen, task_id: str, loop):
    """消费 async generator，投递事件到主 loop。"""
    final_text = ""
    async for evt_type, data in gen:
        if evt_type == "text":
            t = data if isinstance(data, str) else str(data)
            final_text += t
            loop.call_soon_threadsafe(_push_conv_event, task_id, "text", t)
        elif evt_type == "thinking":
            loop.call_soon_threadsafe(_push_conv_event, task_id, "thinking", data)
        elif evt_type == "done":
            if isinstance(data, dict):
                final_text = data.get("reply", "") or final_text
            loop.call_soon_threadsafe(_push_conv_event, task_id, "done", final_text)
    return {"final_text": final_text}


def _push_conv_event(task_id: str, evt_type: str, data):
    """向内存任务缓冲追加事件（在主 event loop 中调用）。"""
    with _conv_tasks_lock:
        t = _conv_tasks.get(task_id)
        if t:
            t["events"].append({"type": evt_type, "data": data})
            if evt_type == "text":
                t["result"] = (t.get("result", "") or "") + (data if isinstance(data, str) else str(data))


@router.get("/chat/task/{task_id}/status")
def conv_task_status(task_id: str, user: dict = Depends(require_no_session_token)):
    """查询对话任务状态（内存优先 → DB 兜底）。"""
    # 1. 内存
    with _conv_tasks_lock:
        task = _conv_tasks.get(task_id)
        if task:
            return {"task_id": task_id, "status": task["status"],
                    "result": task.get("result", ""), "seq": task.get("seq", 0)}

    # 2. DB 兜底（event_thread by task_id）
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT role, generate_status, content FROM event_thread WHERE task_id=? ORDER BY id", (task_id,))
        rows = c.fetchall()
    has_user = any(r[0] == "user" for r in rows)
    has_agent = any(r[0] == "agent" for r in rows)
    if has_agent:
        for r in rows:
            if r[0] == "agent":
                gen = r[1]
                return {"task_id": task_id, "status": "done" if gen == 2 else "error",
                        "result": r[2] or ""}
    if has_user and not has_agent:
        return {"task_id": task_id, "status": "error",
                "result": "", "error": "查询已中断（服务重启），请重新发送消息"}
    return {"task_id": task_id, "status": "not_found"}


@router.get("/chat/task/{task_id}/stream")
async def conv_task_stream(task_id: str, user: dict = Depends(require_no_session_token)):
    """订阅对话任务 SSE 输出（重放缓冲 + 实时追加）。"""
    from fastapi.responses import StreamingResponse

    async def event_stream():
        import json as _json
        sent = 0
        # 重放已有事件
        with _conv_tasks_lock:
            task = _conv_tasks.get(task_id)
            if task:
                events = list(task["events"])
            else:
                events = []
        for evt in events:
            yield f"data: {_json.dumps({'type': evt['type'], 'text': evt['data']})}\n\n"
            sent += 1
        # 如果任务已完成，直接结束
        with _conv_tasks_lock:
            task = _conv_tasks.get(task_id)
            if task and task["status"] in ("done", "error"):
                return
            if not task:
                # 查 DB 看是否有结果
                from src.session_manager import db, db_lock
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT role, generate_status, content FROM event_thread WHERE task_id=? AND role='agent' ORDER BY id DESC LIMIT 1", (task_id,))
                    row = c.fetchone()
                if row:
                    yield f"data: {_json.dumps({'type': 'done', 'text': row[2] or ''})}\n\n"
                else:
                    yield f"data: {_json.dumps({'type': 'error', 'text': '任务不存在'})}\n\n"
                return
        # 实时追加新事件
        import time as _wt
        timeout = _wt.time() + 300  # 5 分钟超时
        while _wt.time() < timeout:
            await asyncio.sleep(0.5)
            with _conv_tasks_lock:
                task = _conv_tasks.get(task_id)
                if not task:
                    break
                new_events = task["events"][sent:]
                sent = len(task["events"])
                done = task["status"] in ("done", "error")
            for evt in new_events:
                yield f"data: {_json.dumps({'type': evt['type'], 'text': evt['data']})}\n\n"
            if done:
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@router.post("/{seq}/knowledge")
def add_knowledge(seq: int, body: KnowledgeBody, request: Request):
    """agent 沉淀共享知识（bot token）：写工作区文件 + 登记索引 + 线程留痕，见 event-conversation-design.md §3.1.1。"""
    evt = _load_event(seq)
    _require_event_bot_token(request, evt["repo_key"])
    if body.scope not in ("project", "issue", "issue_agent"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 project/issue/issue_agent"})
    if not (body.content or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "content 必填"})
    from src.api.knowledge import write_knowledge, _issue_num_from
    issue_num = _issue_num_from(evt["issue_id"])
    task_id = write_knowledge(repo_key=evt["repo_key"], scope=body.scope, content=body.content,
                         status=body.status or "accepted", agent_type=evt["agent_type"],
                         issue_num=issue_num, source_seq=seq, created_by="agent")
    # 对话产出：线程留痕（📌 知识沉淀）
    if task_id:
        _add_thread_message(seq, evt["repo_key"], evt["issue_id"], evt["agent_type"], "system",
                            f"📌 已提交 {body.scope} 级知识沉淀任务：{body.content}")
    return {"ok": True, "scope": body.scope, "task_id": task_id}


@router.post("/{seq}/flow/reset")
def flow_reset(seq: int, body: FlowResetBody, request: Request):
    """agent 流程控制（bot token）：取消该 issue 任务 + 回退阶段 + 线程留痕。见 event-conversation-design.md §3.3③。"""
    evt = _load_event(seq)
    _require_event_bot_token(request, evt["repo_key"])
    issue_id = evt["issue_id"]
    if not issue_id:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "事件无关联 Issue，无法流程回退"})
    from src.api.session_store import get_issue_stage, set_issue_stage
    allowed = {"vod": 0, "issue": 1, "coding": 2, "qa": 3, "sre": 4, "done": 5}
    to_stage = body.to_stage or "vod"
    if to_stage not in allowed:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"非法阶段: {to_stage}"})
    cur = (get_issue_stage(evt["repo_key"], issue_id) or {}).get("stage") or "vod"
    if allowed.get(to_stage, 0) >= allowed.get(cur, 0):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"只能回退到更早阶段（当前 {cur}）"})
    cancelled = _cancel_issue_tasks(evt["repo_key"], issue_id)
    set_issue_stage(evt["repo_key"], issue_id, to_stage, evt["agent_type"])
    # 对话产出：线程留痕（🔄 流程操作）
    _add_thread_message(seq, evt["repo_key"], issue_id, evt["agent_type"], "system",
                        f"🔄 流程干预：回退到 {to_stage} 阶段，取消 {cancelled} 个任务")
    return {"ok": True, "stage": to_stage, "cancelled": cancelled}


@router.get("/{owner}/{repo}")
def list_events(owner: str, repo: str,
                      page: int = Query(1, ge=1),
                      page_size: int = Query(20, ge=1, le=100),
                      status: str = Query("", pattern="^(pending|running|completed|failed)?$"),
                      user: dict = Depends(require_no_session_token)):
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")

    # 权限校验：用户必须有权访问该仓库
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)

    from src.session_manager import db, db_lock

    # 跨平台 repo_key 候选（用户可能通过 GitHub OAuth 登录）
    _owner_repo = f"{owner}/{repo}"
    _repo_keys = [f"gitcode:{_owner_repo}", f"github:{_owner_repo}"]
    _rk_placeholders = ",".join("?" * len(_repo_keys))

    where_clauses = [f"repo_key IN ({_rk_placeholders})"]
    params = _repo_keys
    if status:
        where_clauses.append("status=?")
        params.append(status)

    where_sql = " AND ".join(where_clauses)

    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"SELECT COUNT(*) FROM events WHERE {where_sql}", params)
            total = c.fetchone()[0]

            offset = (page - 1) * page_size
            c.execute(
                f"SELECT seq, event_type, agent_type, title, summary, status, created_at FROM events WHERE {where_sql} ORDER BY seq DESC LIMIT ? OFFSET ?",
                params + [page_size, offset]
            )
            events = [
                {"seq": r[0], "event_type": r[1], "agent_type": r[2] or "", "title": r[3] or r[1],
                 "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": normalize_event_time(r[6])}
                for r in c.fetchall()
            ]
        except Exception:
            return {"events": [], "total": 0, "page": page, "page_size": page_size}

    return {"events": events, "total": total, "page": page, "page_size": page_size}
