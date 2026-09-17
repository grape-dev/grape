"""Task 运行日志路由：SSE 实时流 + 增量拉取（实时日志功能，对齐 multica TaskMessage 广播模型）。

- GET /api/task-logs/{task_id}/stream?since=<seq> — SSE 实时流（先发存量，再订阅 fanout）
- GET /api/task-logs/{task_id}/messages?since=<seq> — 增量拉取（断线/轮询兜底）

鉴权：登录用户（Bearer token）。前端用 fetch + ReadableStream 消费（同 chat SSE 模式）。
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.api.deps import require_no_session_token
from src.config import log
from src.runtime.service import list_task_messages

router = APIRouter(prefix="/api/task-logs", tags=["task-logs"])

# 无消息时的心跳间隔：防代理/浏览器超时断连
_HEARTBEAT_S = 15


def _require_task_exists(task_id: str):
    from src.runtime.service import get_task
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})


@router.get("/{task_id}/info", operation_id="task_logs_info", summary="Task runtime info")
def get_task_runtime_info(task_id: str, user: dict = Depends(require_no_session_token)):
    """任务关联的执行引擎/机器信息 + 接收上下文（prompt）。

    返回：{agent_name, agent_version, device_name, last_ip, owner_name, prompt, prompt_full_length,
           created_at, started_at, completed_at, status, issue_id, event_seq, repo_key}
    agent_runtimes.name/version（引擎名+版本号）、runtime_credentials.device_name/last_ip/owner_name。
    prompt 为 agent 接收到的完整上下文（截断至 8000 字符），prompt_full_length 为原始长度。
    """
    from src.session_manager import db, db_lock
    from src.api.time_utils import normalize_event_time
    with db_lock:
        c = db.cursor()
        c.execute(
            """SELECT ar.name, ar.version, rc.device_name, rc.last_ip, rc.owner_name, t.prompt,
                      t.created_at, t.started_at, t.completed_at, t.status, t.issue_id, t.event_seq, t.repo_key,
                      rc.cpu_percent, rc.memory_percent, rc.disk_percent, rc.load_avg, rc.spec
               FROM agent_tasks t
               LEFT JOIN agent_runtimes ar ON ar.id = t.runtime_id
               LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
               WHERE t.id = ?""",
            (task_id,),
        )
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    agent_name, agent_version, device_name, last_ip, owner_name, prompt = row[:6]
    created_at, started_at, completed_at, status, issue_id, event_seq, repo_key = row[6:13]
    cpu_pct, mem_pct, disk_pct, load_avg, spec = row[13:18] if len(row) > 17 else (0, 0, 0, "", "")
    import re as _re
    _m = _re.search(r"(\d+\.\d+\.\d+)", str(agent_version or ""))
    ver = _m.group(1) if _m else str(agent_version or "")
    _prompt = prompt or ""
    _PROMPT_CAP = 8000
    return {
        "agent_name": agent_name or "",
        "agent_version": ver,
        "device_name": device_name or "",
        "last_ip": last_ip or "",
        "owner_name": owner_name or "",
        "prompt": _prompt[:_PROMPT_CAP],
        "prompt_full_length": len(_prompt),
        "created_at": normalize_event_time(created_at),
        "started_at": normalize_event_time(started_at),
        "completed_at": normalize_event_time(completed_at),
        "status": status or "",
        "issue_id": issue_id or "",
        "event_seq": event_seq or 0,
        "repo_key": repo_key or "",
        "spec": spec or "",
        "cpu_percent": cpu_pct or 0,
        "memory_percent": mem_pct or 0,
        "disk_percent": disk_pct or 0,
        "load_avg": load_avg or "",
    }


@router.get("/{task_id}/messages", operation_id="task_logs_messages", summary="Task messages")
def get_task_messages(task_id: str, user: dict = Depends(require_no_session_token),
                            since: int = Query(0, ge=0)):
    """增量拉取：since 之后的消息（seq 单调递增）。"""
    _require_task_exists(task_id)
    return list_task_messages(task_id, since=since)


@router.get("/{task_id}/stream", operation_id="task_logs_stream", summary="Task message stream (SSE)")
async def stream_task_messages(task_id: str, request: Request,
                               user: dict = Depends(require_no_session_token),
                               since: int = Query(0, ge=0)):
    """SSE 实时流：先发 since 之后存量 → 阻塞读订阅队列 → 心跳保活。"""
    _require_task_exists(task_id)
    from src.runtime.task_log_stream import subscribe, unsubscribe, sse_frame

    async def gen():
        # 1) 存量（断线/新开页 catch-up，seq 去重由前端 merge 保证）
        try:
            for m in list_task_messages(task_id, since=since):
                yield sse_frame(m)
        except Exception as e:
            log.warning(f"task-log stream: catch-up failed: {e}")
        # 2) 实时订阅
        q = await subscribe(task_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_S)
                    yield sse_frame(payload)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            await unsubscribe(task_id, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关 nginx 缓冲，保证流式实时
        },
    )
