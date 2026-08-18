"""运行日志实时推送：task_id → asyncio.Queue 订阅表（SSE fanout）。

设计（对齐 multica TaskMessage 广播模型，grape 用 SSE 而非 WS）：
- 服务端进程内维护 task_id → set[asyncio.Queue] 订阅表
- worker 上报消息落库后 fanout 到订阅队列（队列满丢弃保活，SSE 重连靠 ?since= 补数据）
- SSE 端点（src/api/routes/task_logs.py）先发存量、再阻塞读队列
- DB 是唯一事实源，实时推送只是加速；断线/新开页用 GET /messages?since= 增量回源
"""
import asyncio
import json
from collections import defaultdict

from src.config import log

_subscribers: dict[str, set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]]] = defaultdict(set)
_lock = asyncio.Lock()

# 单队列容量：消息量极大时丢弃最旧（实时性优先，补数据靠 since 回源）
_QUEUE_MAX = 500


async def subscribe(task_id: str) -> asyncio.Queue:
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    async with _lock:
        _subscribers[task_id].add((q, loop))
    return q


async def unsubscribe(task_id: str, q: asyncio.Queue):
    async with _lock:
        s = _subscribers.get(task_id)
        if s:
            s.discard(next((t for t in s if t[0] is q), None))
            if not s:
                _subscribers.pop(task_id, None)


def fanout(task_id: str, messages: list):
    """同步 fanout（report_messages 落库后调用，任意线程安全）。

    put 走订阅者 loop 的 call_soon_threadsafe——fanout 可能从 ingestion 循环
    （uvicorn loop）或同步兜底路径（任意线程）被调用，跨线程 put 必须线程安全。
    队列满丢弃最旧一条再放（实时通道允许丢，历史靠 DB + since 回源）。
    """
    s = _subscribers.get(task_id)
    if not s:
        return
    for m in messages:
        if not isinstance(m, dict):
            continue
        payload = _to_payload(m)
        if payload is None:
            continue
        for q, loop in list(s):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            if loop.is_running():
                loop.call_soon_threadsafe(q.put_nowait, payload)
            else:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass


def _to_payload(m: dict) -> dict | None:
    """worker 消息 {type, data} → 前端 SSE payload（seq 由 DB 权威生成，此处不带）。

    返回 None 表示无需推送（done 等终态事件由事件轮询兜底）。
    created_at 取服务端接收时刻（UTC ISO）：worker 消息不带时间戳，落库与 fanout
    同在 report_messages 内执行，与 task_messages.created_at 同源（时间本地化见
    docs/event-time-localization-design.md）。
    """
    t = m.get("type", "log")
    if t == "done":
        return None
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    d = m.get("data")
    if isinstance(d, dict):
        tool = str(d.get("tool") or "")
        if t == "tool_use":
            try:
                inp = json.loads(d.get("input") or "{}") if isinstance(d.get("input"), str) else (d.get("input") or {})
            except Exception:
                inp = {}
            return {"type": t, "tool": tool, "content": "", "input": inp, "output": "", "created_at": _now}
        content = str(d.get("text") or d.get("content") or "")
        return {"type": t, "tool": tool, "content": content, "input": {}, "output": "", "created_at": _now}
    return {"type": t, "tool": "", "content": str(d or ""), "input": {}, "output": "", "created_at": _now}


def sse_frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
