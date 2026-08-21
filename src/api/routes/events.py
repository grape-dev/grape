"""Events 路由：事件列表、详情、Event 对话（event-conversation-design.md）"""
import asyncio
import os
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.config import log
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/detail/{seq}")
async def get_event_detail(seq: int, user: dict = Depends(get_current_user)):
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
        "repo_key": from_repo_key(row[1] or ""),
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


@router.post("/{seq}/redispatch")
async def redispatch_event(seq: int, user: dict = Depends(get_current_user)):
    """手动重新派发事件任务（卡死 running>10min / failed 时人工收敛）。

    见 event-redispatch-design.md §4.1.2：
    - 关联任务非终态（dispatched/running）→ requeue 置回 queued；
    - 关联任务终态/缺失（含事件 failed）→ 复用原 task 快照参数重新 enqueue_task；
    - 事件状态回写 running。30s 内重复调用幂等拒绝。
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

    # 2) 查关联任务
    task_id = ""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, status FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1", (seq,))
        tr = c.fetchone()
        if tr:
            task_id, task_status = tr[0], tr[1] or ""

    # 3) 分支处理
    if task_id and task_status in ("dispatched", "running"):
        # 非终态 → requeue 置回 queued，worker 重新 claim
        requeue_task(task_id)
    else:
        # 终态/缺失 → 复用原 task 快照参数新建任务（新 task_id，清空 runtime_session_id）
        if task_id:
            old = get_task(task_id)
            if old:
                _sid = old.get("issue_session_id") or ""
                _env = {}
                try:
                    _env = _json.loads(old.get("env_extra") or "{}")
                except Exception:
                    _env = {}
                # 重派时重新解析路由：旧 task 快照的 runtime_id 可能为 0（120s 超时回收产物，
                # 触发 enqueue_task D3 校验失败）。复用 pipeline 同款路由逻辑（pick_engine：
                # 引擎集合 → 专属/全局/本机；无则回退旧快照 runtime_id，仍无则拒派）。
                _rid = old.get("runtime_id") or 0
                try:
                    from src.runtime.engine_select import pick_engine
                    _rid = pick_engine(old.get("repo_key") or "", old.get("agent_type") or "", fallback_runtime_id=_rid) or 0
                except Exception:
                    _rid = 0
                if not _rid:
                    raise HTTPException(status_code=409, detail={"code": 409, "message": "无可用引擎，无法重派"})
                # cwd 按 Issue 统一隔离重算（workdir-issue-centric-design.md §二），走新结构路径
                try:
                    from src.api.event_pipeline import get_agent_workdir
                    _cwd = get_agent_workdir(old.get("repo_key") or "", old.get("agent_type") or "",
                                            old.get("issue_id") or "")
                except Exception:
                    _cwd = old.get("cwd") or ""
                enqueue_task(
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
                    # 从 issue_agent_session 读取 runtime_session_id（保留会话连续性），
                    # 空时走全新会话。执行端检测连续失败后自动清空（smart-session-recovery-design.md）
                    runtime_session_id=old.get("runtime_session_id") or "",
                    event_type=old.get("event_type") or "",
                    account=old.get("account") or "",
                    user_id=old.get("user_id") or 0,
                    repo_gitcode_token=old.get("repo_gitcode_token") or "",
                    env_extra=_env,
                    # skill_files / project_info / knowledge 不传 → enqueue 内部重新打包
                    server_url=old.get("server_url") or "",
                    issue_session_id=_sid,
                )
                # 取消旧任务（重派创建新任务后，旧任务不应保留 queued/running/dispatched——
                # 否则 _running_issue_ids 仍会将其计入执行态，或以旧 runtime_id 被重复 claim）
                try:
                    update_task_status(task_id, "cancelled")
                except Exception:
                    pass
            else:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "关联任务不存在，无法重派"})
        else:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "该事件无关联任务，无法重派"})

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
                "SELECT seq, repo_key, issue_id, agent_type, event_type, title, result, status FROM events WHERE seq=?",
                (seq,))
            row = c.fetchone()
        except Exception:
            row = None
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件未找到"})
    return {"seq": row[0], "repo_key": row[1], "issue_id": row[2] or "", "agent_type": row[3] or "",
            "event_type": row[4] or "", "title": row[5] or "", "result": row[6] or "", "status": row[7] or ""}


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
                        role: str, content: str, created_by: str = ""):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO event_thread (anchor_seq, repo_key, issue_id, agent_type, role, content, created_by) VALUES (?,?,?,?,?,?,?)",
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
    """agent 端点鉴权：与 a2a_endpoint 同款（仓库 bot token）。"""
    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not token:
        token = request.headers.get("X-Bot-Token", "") or ""
    from src.user.db import get_repo_bot_token_no_global
    expected = get_repo_bot_token_no_global(repo_key) or ""
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "invalid bot token"})


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


async def _direct_attempt(prompt, ws, session_id, repository, bot_token, anchor_seq, repo_key, agent_type):
    """直接模式单次执行：yield (text/thinking) 事件；以 sentinel 结束（_stuck / _done）。"""
    from src.runtime.executor import consume_stream_async
    acc = ""
    new_oc = ""
    gen = consume_stream_async(
        prompt, ws, session_id=session_id, event_type="event_conversation",
        backend_name="opencode", account=repository, bot_token=bot_token,
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


async def _run_conversation(evt: dict, user_message: str, username: str):
    """门禁已通过：写用户消息 → 复用 issue 会话运行 agent → 回写 agent 回复。

    生成器事件：(thinking, text) / (text, 增量) / (done, {reply, messages})。
    - direct：SSE 流式（consume_stream_async）
    - 队列模式：入队 + 轮询（knowledge_files 由 enqueue_task 打包，见 runtime/service.py）
    """
    from src.config import WORKSPACE_DIR
    from src.repo_key import from_repo_key
    from src.api.knowledge import _issue_num_from
    repo_key, agent_type, issue_id, anchor_seq = evt["repo_key"], evt["agent_type"], evt["issue_id"], evt["seq"]

    _add_thread_message(anchor_seq, repo_key, issue_id, agent_type, "user", user_message, username)
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
    prompt = build_conversation_prompt(evt, user_message, _list_thread(anchor_seq), agent_type, issue_num)

    from src.user.db import get_gitcode_token
    bot_token = get_gitcode_token(repo_key=repo_key) or ""
    repository = from_repo_key(repo_key)

    from src.runtime.service import get_execution_mode, EXEC_MODE_DIRECT, enqueue_task, wait_task_terminal, cancel_task
    final_text = ""
    new_oc = oc_session_id

    if get_execution_mode() != EXEC_MODE_DIRECT:
        # 队列模式：入队 + 轮询（打字机效果降级，任务终态一次返回）
        # 技能随任务下发（Fix A）：event-dialogue 打包进 skill_files → worker 物化到 DEVOPS_SKILLS_DIR
        yield ("thinking", "任务已入队，等待 worker 执行...")
        runtime_id = 0
        agent_sid = ""
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, runtime_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                      (repo_key, agent_type))
            row = c.fetchone()
        if row:
            agent_sid = row[0] or ""
            runtime_id = row[1] or 0
        if agent_sid:
            from src.config import SKILLS_DIR
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
                final_text = "（对话执行失败：无可用引擎/会话）"
            else:
                tid = enqueue_task(
                    agent_session_id=agent_sid, runtime_id=runtime_id, prompt=prompt, cwd=ws,
                    runtime_session_id=oc_session_id,
                    issue_session_id=issue_sess_id,        # Fix E：串行槽绑定本 issue 会话
                    event_type="event_conversation",
                    repo_key=repo_key, agent_type=agent_type,
                    skill="event-dialogue",                 # Fix A：打包下发对话技能
                    skill_path=os.path.join(SKILLS_DIR, "event-dialogue", "SKILL.md"),
                    account=repository, repo_gitcode_token=bot_token,
                    issue_id=issue_id or "#0",
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
    _add_thread_message(anchor_seq, repo_key, issue_id, agent_type, "agent", final_text, agent_type)
    yield ("done", {"reply": final_text, "messages": _list_thread(anchor_seq)})


@router.get("/{seq}/thread")
async def get_event_thread(seq: int, user: dict = Depends(get_current_user)):
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


@router.post("/{seq}/chat")
async def send_conversation(seq: int, body: ConversationMessage, user: dict = Depends(get_current_user)):
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
async def stream_conversation(seq: int, body: ConversationMessage, user: dict = Depends(get_current_user)):
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


@router.post("/{seq}/knowledge")
async def add_knowledge(seq: int, body: KnowledgeBody, request: Request):
    """agent 沉淀共享知识（bot token）：写工作区文件 + 登记索引 + 线程留痕，见 event-conversation-design.md §3.1.1。"""
    evt = _load_event(seq)
    _require_event_bot_token(request, evt["repo_key"])
    if body.scope not in ("project", "issue", "issue_agent"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 project/issue/issue_agent"})
    if not (body.content or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "content 必填"})
    from src.api.knowledge import write_knowledge, _issue_num_from
    issue_num = _issue_num_from(evt["issue_id"])
    fp = write_knowledge(repo_key=evt["repo_key"], scope=body.scope, content=body.content,
                         status=body.status or "accepted", agent_type=evt["agent_type"],
                         issue_num=issue_num, source_seq=seq, created_by="agent")
    # 对话产出：线程留痕（📌 知识沉淀）
    _add_thread_message(seq, evt["repo_key"], evt["issue_id"], evt["agent_type"], "system",
                        f"📌 已沉淀 {body.scope} 级共享知识：{body.content}")
    return {"ok": True, "scope": body.scope, "file_path": fp}


@router.post("/{seq}/flow/reset")
async def flow_reset(seq: int, body: FlowResetBody, request: Request):
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
async def list_events(owner: str, repo: str,
                      page: int = Query(1, ge=1),
                      page_size: int = Query(20, ge=1, le=100),
                      status: str = Query("", pattern="^(pending|running|completed|failed)?$"),
                      user: dict = Depends(get_current_user)):
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")
    from src.session_manager import db, db_lock

    where_clauses = ["repo_key=?"]
    params = [repo_key]
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
