"""Events 路由：事件列表、详情、Event 对话（event-conversation-design.md）"""
import os
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel
from src.api.deps import get_current_user

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
        "created_at": row[11],
        "runtime": row[12] or "",
        "skill": row[13] or "",
        "exit_code": row[14],
        "duration": row[15],
        "agent_logs": agent_logs,
        "started_at": row[17] or "",
        "completed_at": row[18] or "",
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
    return result


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
    return [{"id": r[0], "role": r[1], "content": r[2], "created_by": r[3] or "", "created_at": r[4]} for r in rows]


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

    from src.runtime.service import get_execution_mode, EXEC_MODE_DIRECT, enqueue_task, wait_task_terminal
    final_text = ""
    new_oc = oc_session_id

    if get_execution_mode() != EXEC_MODE_DIRECT:
        # 队列模式：入队 + 轮询（打字机效果降级，任务终态一次返回）
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
        if agent_sid and runtime_id:
            ws = os.path.join(WORKSPACE_DIR, repository, "event-conv")
            os.makedirs(ws, exist_ok=True)
            tid = enqueue_task(
                agent_session_id=agent_sid, runtime_id=runtime_id, prompt=prompt, cwd=ws,
                runtime_session_id=oc_session_id, event_type="event_conversation",
                repo_key=repo_key, agent_type=agent_type, skill="",
                account=repository, repo_gitcode_token=bot_token,
                issue_id=issue_id or "#0",
            )
            task = await wait_task_terminal(tid)
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
        # direct：SSE 流式
        ws = os.path.join(WORKSPACE_DIR, repository, "event-conv")
        os.makedirs(ws, exist_ok=True)
        from src.runtime.executor import consume_stream_async
        acc = ""
        try:
            async for evt_type, data in consume_stream_async(
                prompt, ws, session_id=oc_session_id, event_type="event_conversation",
                backend_name="opencode", account=repository, bot_token=bot_token,
                agent_name="Event Dialogue Agent",
                agent_label=f"[event-conv|repo:{repo_key}|seq:{anchor_seq}]",
            ):
                if evt_type == "text":
                    t = data.get("text") if isinstance(data, dict) else str(data)
                    if t:
                        acc += t
                        yield ("text", t)
                elif evt_type == "reasoning":
                    yield ("thinking", data.get("text") if isinstance(data, dict) else str(data))
                elif evt_type == "done" and isinstance(data, dict):
                    final_text = data.get("text", "") or acc
                    new_oc = data.get("oc_session_id", "") or new_oc
        except Exception as e:
            final_text = f"（对话执行失败：{e}）"
        if not final_text and acc:
            final_text = acc

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
                 "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": r[6]}
                for r in c.fetchall()
            ]
        except Exception:
            return {"events": [], "total": 0, "page": page, "page_size": page_size}

    return {"events": events, "total": total, "page": page, "page_size": page_size}
