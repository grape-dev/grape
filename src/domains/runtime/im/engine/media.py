"""MediaWorkQueue（Issue #760 P1，P4 修正落地）——VLM 媒体理解挪出 handler 协程。

流水线（Router ⑦ 前置步）仅入队 MediaRef 引用（非阻塞）；
下载/ffmpeg/VLM 理解在 worker 任务异步执行，描述结果经 on_result
回调补进会话上下文（TTL 缓存语义由调用方保留，§4.3）。

失败纪律：单个 MediaRef 理解失败记日志、不扩散、不移位
（multica 陷阱 7：附件失败不得移位后续附件索引）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from ..channel.message import MediaRef

log = logging.getLogger(__name__)

PROCESSED_MAX = 2000  # 测试观察哨上限（超出裁剪至一半，防生产实例无限增长）


class MediaWorkQueue:
    """媒体理解工作队列：enqueue 非阻塞，worker 侧串行消费。

    describe:  worker 侧理解（VLM）——Callable[[MediaRef, ctx], Awaitable[str]]
    on_result: 描述落会话上下文——
               Callable[[installation_id, binding_key, MediaRef, str], Awaitable[None]]
    """

    def __init__(
        self,
        describe: Callable[..., Awaitable[str]],
        on_result: Callable[..., Awaitable[None]],
        *,
        concurrency: int = 4,
    ) -> None:
        self._describe = describe
        self._on_result = on_result
        self._queue: asyncio.Queue = asyncio.Queue()
        self._concurrency = concurrency
        self._workers: list[asyncio.Task] = []
        self.processed: list = []   # 测试观察哨

    def enqueue(self, installation_id: str, binding_key: str,
                media_refs: list[MediaRef], context: dict | None = None) -> None:
        """入队（非阻塞）：流水线立即继续，理解异步发生。"""
        for ref in media_refs or []:
            self._queue.put_nowait((installation_id, binding_key, ref, context or {}))

    async def start(self) -> None:
        for i in range(self._concurrency):
            self._workers.append(asyncio.get_running_loop().create_task(
                self._worker(), name=f"media-worker-{i}"))

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()

    async def drain(self) -> None:
        """测试/优雅停机：等队列清空且在途任务完成。"""
        await self._queue.join()

    async def _worker(self) -> None:
        while True:
            installation_id, binding_key, ref, context = await self._queue.get()
            try:
                description = await self._describe(ref, context)
                await self._on_result(installation_id, binding_key, ref, description)
                self.processed.append((installation_id, binding_key, ref.storage_key, description))
                if len(self.processed) > PROCESSED_MAX:
                    del self.processed[: int(PROCESSED_MAX / 2)]
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 失败隔离：记日志不扩散
                log.warning("media describe failed for %s: %r", ref.storage_key, e)
            finally:
                self._queue.task_done()

    @property
    def pending(self) -> int:
        return self._queue.qsize()


__all__ = ["MediaWorkQueue"]
