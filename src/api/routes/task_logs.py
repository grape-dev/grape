"""Task 运行日志路由：SSE 实时流 + 增量拉取（实时日志功能，对齐 multica TaskMessage 广播模型）。

- GET /api/task-logs/{task_id}/stream?since=<seq> — SSE 实时流（先发存量，再订阅 fanout）
- GET /api/task-logs/{task_id}/messages?since=<seq> — 增量拉取（断线/轮询兜底）

鉴权：登录用户（Bearer token）。前端用 fetch + ReadableStream 消费（同 chat SSE 模式）。
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.api.deps import get_current_user
from src.config import log
from src.runtime.service import list_task_messages

router = APIRouter(prefix="/api/task-logs", tags=["task-logs"])

# 无消息时的心跳间隔：防代理/浏览器超时断连
_HEARTBEAT_S = 15


def _require_task_exists(task_id: str):
    from src.runtime.service import get_task
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})


@router.get("/{task_id}/messages")
async def get_task_messages(task_id: str, user: dict = Depends(get_current_user),
                            since: int = Query(0, ge=0)):
    """增量拉取：since 之后的消息（seq 单调递增）。"""
    _require_task_exists(task_id)
    return list_task_messages(task_id, since=since)


@router.get("/{task_id}/stream")
async def stream_task_messages(task_id: str, request: Request,
                               user: dict = Depends(get_current_user),
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
