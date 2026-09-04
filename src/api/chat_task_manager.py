"""Chat Agent 内存任务管理器 — submit/stream 分离 + 缓冲重放。

学习 Admin AI AiTaskManager 机制：
1. submit 启动后台线程执行 agent，立即返回
2. SSE 订阅可断开重连 — 先重放内存缓冲，再实时追加
3. 任务完成写 DB（finally 保证），释放信号量
4. 30 分钟定时清理已完成任务

线程安全设计：
- _tasks / _lock: 全局任务表 + 读写锁
- ChatTask.events: deque 线程安全 append
- ChatTask.subscribers: list，通过 _lock 保护
- asyncio.Queue.put_nowait 通过 loop.call_soon_threadsafe 跨线程调用
"""
import threading
import time
import asyncio
import json
from collections import deque
from typing import Callable, Generator, Any
from src.config import log


class ChatTask:
    """单个 chat 任务的内存状态"""

    def __init__(self, task_id: str, conv_id: str, user_id: int, username: str):
        self.task_id = task_id
        self.conv_id = conv_id
        self.user_id = user_id
        self.username = username
        self.status = "running"  # running / done / error
        self.events: deque[str] = deque(maxlen=500)  # SSE 事件缓冲（JSON 字符串）
        self.subscribers: list[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []
        self.created_at = time.time()
        self.finished_at = 0.0
        self.final_text = ""
        self.error = ""
        self._lock = threading.Lock()

    def add_subscriber(self, q: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        with self._lock:
            self.subscribers.append((q, loop))

    def remove_subscriber(self, q: asyncio.Queue):
        with self._lock:
            self.subscribers = [(sq, sl) for sq, sl in self.subscribers if sq is not q]

    def push_event(self, evt_type: str, data: Any):
        """事件 → 缓冲 + flush 到所有 subscribers（线程安全）"""
        # 序列化为 SSE data 行
        if isinstance(data, str):
            text = data
        elif isinstance(data, dict):
            text = data.get("text", str(data))
        else:
            text = str(data)
        sse_line = json.dumps({"type": evt_type, "text": text}, ensure_ascii=False)
        self.events.append(sse_line)
        with self._lock:
            subs = list(self.subscribers)
        for q, loop in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, sse_line)
            except Exception:
                pass  # subscriber 可能已关闭

    def notify_done(self):
        """通知所有 subscribers 任务完成"""
        with self._lock:
            subs = list(self.subscribers)
        for q, loop in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, None)  # None = 结束信号
            except Exception:
                pass
        with self._lock:
            self.subscribers.clear()


class ChatTaskManager:
    """内存任务管理器 — submit/stream 分离 + 缓冲重放"""

    _tasks: dict[str, ChatTask] = {}
    _lock = threading.Lock()
    _semaphore = threading.Semaphore(20)  # 并发限制
    _cleanup_started = False

    @classmethod
    def _ensure_cleanup(cls):
        """启动定时清理线程（只启动一次）"""
        if cls._cleanup_started:
            return
        cls._cleanup_started = True

        def _cleanup_loop():
            while True:
                time.sleep(30 * 60)  # 30 分钟
                cls._cleanup_stale()

        t = threading.Thread(target=_cleanup_loop, daemon=True)
        t.start()

    @classmethod
    def _cleanup_stale(cls):
        """清理 30 分钟前已完成的任务"""
        cutoff = time.time() - 30 * 60
        with cls._lock:
            stale = [
                tid for tid, t in cls._tasks.items()
                if t.status != "running" and t.finished_at > 0 and t.finished_at < cutoff
            ]
            for tid in stale:
                del cls._tasks[tid]
        if stale:
            log.info(f"ChatTaskManager cleanup {len(stale)} stale tasks")

    @classmethod
    def submit(cls, task_id: str, conv_id: str, user_id: int, username: str,
               agent_fn: Callable[[], Generator]) -> ChatTask:
        """启动后台 agent 线程，立即返回 ChatTask。

        agent_fn 是一个 generator，yield (evt_type, data) 事件。
        """
        cls._ensure_cleanup()
        with cls._lock:
            task = ChatTask(task_id, conv_id, user_id, username)
            cls._tasks[task_id] = task

        t = threading.Thread(target=cls._run, args=(task, agent_fn), daemon=True,
                           name=f"chat-agent-{task_id}")
        t.start()
        return task

    @classmethod
    def _run(cls, task: ChatTask, agent_fn: Callable[[], Generator]):
        """后台线程：执行 agent_fn，事件推入缓冲 + subscribers"""
        if not cls._semaphore.acquire(blocking=True, timeout=30):
            cls._set_error(task, "并发超限（信号量获取超时）")
            return
        log.info(f"[ChatTaskManager] agent_worker starting task={task.task_id} conv={task.conv_id}")
        try:
            for evt_type, data in agent_fn():
                task.push_event(evt_type, data)
                if evt_type == "done":
                    if isinstance(data, dict):
                        task.final_text = data.get("text", "")
                    elif isinstance(data, str):
                        task.final_text = data
                    # done 有有效文本 → 覆盖 error 状态；done 无文本但有 error → 保留 error
                    if task.final_text:
                        task.status = "done"
                    elif task.error:
                        task.status = "error"
                    else:
                        task.status = "done"
                    break
                elif evt_type == "error":
                    # 不 break — 让 agent_fn 继续执行到 done，
                    # 使 stream_agent 内部的 session 失效重试逻辑有机会生效。
                    task.error = str(data)
                    if task.status != "done":
                        task.status = "error"
            # agent_fn 正常结束但未发 done → 收集到的文本作为结果
            if task.status == "running":
                task.status = "done" if task.final_text else "error"
        except Exception as e:
            import traceback
            log.warning(f"[ChatTaskManager] agent_worker EXC: {e}\n{traceback.format_exc()}")
            cls._set_error(task, str(e))
        finally:
            cls._semaphore.release()
            task.finished_at = time.time()
            task.notify_done()
            log.info(f"[ChatTaskManager] agent_worker finished task={task.task_id} status={task.status} "
                     f"text_len={len(task.final_text)}")

    @classmethod
    def _set_error(cls, task: ChatTask, error: str):
        task.error = error
        task.status = "error"
        task.push_event("error", error)

    @classmethod
    def subscribe(cls, task_id: str, loop: asyncio.AbstractEventLoop) -> tuple[asyncio.Queue | None, ChatTask | None]:
        """订阅任务输出 — 返回 (queue, task)。

        先重放缓冲，再实时追加。
        - 内存有任务 → 重放 events + 加入 subscribers
        - 内存无任务 → 返回 (None, None)，调用方走 DB 兜底
        """
        with cls._lock:
            task = cls._tasks.get(task_id)
        if not task:
            return None, None

        q: asyncio.Queue = asyncio.Queue()
        # 重放缓冲
        for evt in task.events:
            q.put_nowait(evt)
        if task.status != "running":
            # 已完成 → 重放后发结束信号
            q.put_nowait(None)
            return q, task
        # 运行中 → 加入 subscribers 接收后续事件
        task.add_subscriber(q, loop)
        return q, task

    @classmethod
    def get_status(cls, task_id: str) -> dict | None:
        """查询任务状态（内存优先）。

        返回 {"status", "text", "error"} 或 None（内存无）。
        """
        with cls._lock:
            task = cls._tasks.get(task_id)
        if not task:
            return None
        return {
            "status": task.status,
            "text": task.final_text,
            "error": task.error,
        }

    @classmethod
    def unsubscribe(cls, task_id: str, q: asyncio.Queue):
        """SSE 连接断开时移除 subscriber（防止 push 到已关闭的 queue）"""
        with cls._lock:
            task = cls._tasks.get(task_id)
        if task:
            task.remove_subscriber(q)
