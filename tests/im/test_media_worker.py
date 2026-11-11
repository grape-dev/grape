"""MediaWorkQueue 测试（Issue #760 P1——P4 修正：VLM 挪 worker 侧）。"""
import asyncio

import pytest

from src.im.channel.message import MediaRef, MsgType
from src.im.channel.types import ChatType, Type
from src.im.engine.media import MediaWorkQueue
from src.im.engine.router import Router, Installation, Identity
from src.im.engine.dedup import InboundDedup, InMemoryDedupStore
from src.im.engine.session import InMemoryBindingStore
from src.im.channel.message import InboundMessage, Source


def _msg(mid, media=None):
    return InboundMessage(
        event_id=f"evt-{mid}", message_id=mid,
        source=Source(channel_type=Type.FEISHU, chat_id="oc1",
                      chat_type=ChatType.P2P, sender_id="ou1"),
        type=MsgType.MEDIA if media else MsgType.TEXT,
        text="[视频]" if media else "hi",
        media_refs=media or [],
    )


def _router(media_worker):
    async def _inst(msg):
        return Installation(installation_id="i1", channel_type="feishu", scope="repo", repo_key="a/b")

    async def _identity(inst, msg):
        return Identity(user_id="u1", unbound=False)

    return Router(
        installations=_inst, identity=_identity,
        bindings=InMemoryBindingStore(), dedup=InboundDedup(InMemoryDedupStore()),
        replier=type("R", (), {"send_binding_card": staticmethod(asyncio.sleep(0))})(),
        media_worker=media_worker,
        agent_run=None,
    )


async def _test_enqueue_does_not_block_pipeline_impl():
    """P4 回归：入队即时返回，流水线不等 VLM。"""
    started = asyncio.Event()

    async def describe(ref, ctx):
        started.set()
        await asyncio.sleep(10)

    results = []

    async def on_result(*a):
        results.append(a)

    q = MediaWorkQueue(describe, on_result)
    router = _router(q)
    t0 = asyncio.get_event_loop().time()
    await router.handle(None, _msg("m1", [MediaRef(type=MsgType.MEDIA, storage_key="file_v1")]))
    elapsed = asyncio.get_event_loop().time() - t0
    assert elapsed < 0.5
    assert q.pending == 1


async def _test_worker_describes_and_delivers_result_impl():
    """describe → on_result 链路：描述补进会话上下文（回调收到）。"""
    results = []

    async def describe(ref, ctx):
        return "a video about tests"

    async def on_result(installation_id, binding_key, ref, description):
        results.append((installation_id, binding_key, ref.storage_key, description))

    q = MediaWorkQueue(describe, on_result)
    await q.start()
    q.enqueue("i1", "oc1", [MediaRef(type=MsgType.MEDIA, storage_key="file_v1")])
    await asyncio.wait_for(q.drain(), timeout=2)
    await q.stop()
    assert results == [("i1", "oc1", "file_v1", "a video about tests")]
    assert q.processed


async def _test_worker_failure_isolated_impl():
    """单个 MediaRef 理解失败不扩散、不移位后续附件。"""
    results = []

    async def describe(ref, ctx):
        if ref.storage_key == "bad":
            raise RuntimeError("vlm down")
        return "ok:" + ref.storage_key

    async def on_result(installation_id, binding_key, ref, description):
        results.append((ref.storage_key, description))

    q = MediaWorkQueue(describe, on_result)
    await q.start()
    q.enqueue("i1", "oc1", [
        MediaRef(type=MsgType.MEDIA, storage_key="bad", inline_index=0),
        MediaRef(type=MsgType.IMAGE, storage_key="img1", inline_index=1),
    ])
    await asyncio.wait_for(q.drain(), timeout=2)
    await q.stop()
    # 失败项不产生 result；后续附件不受影响（不移位）
    assert results == [("img1", "ok:img1")]


async def _test_router_enqueues_to_queue_impl():
    """Router 流水线 ⑦ 前置步接通：media_enqueued 观察哨 + 队列收到引用。"""
    results = []

    async def describe(ref, ctx):
        return "d"

    async def on_result(*a):
        results.append(a)

    q = MediaWorkQueue(describe, on_result)
    router = _router(q)
    await router.handle(None, _msg("m2", [MediaRef(type=MsgType.IMAGE, storage_key="img_v2")]))
    assert ("media_enqueued", "evt-m2") in router.calls
    assert q.pending == 1
    await q.start()
    await asyncio.wait_for(q.drain(), timeout=2)
    await q.stop()
    assert len(results) == 1
    assert results[0][2].storage_key == "img_v2"


async def _test_worker_cancel_calls_task_done_once_impl():
    """取消路径 task_done 仅一次（finally 唯一调用点）：
    worker 被取消后以 CancelledError 终止（不抛 ValueError），
    drain()/queue.join() 仍能完成。"""
    started = asyncio.Event()

    async def describe(ref, ctx):
        started.set()
        await asyncio.sleep(10)

    async def on_result(*a):
        pass

    q = MediaWorkQueue(describe, on_result, concurrency=1)
    await q.start()
    q.enqueue("i1", "oc1", [MediaRef(type=MsgType.MEDIA, storage_key="file_v1")])
    await asyncio.wait_for(started.wait(), timeout=2)
    worker = q._workers[0]
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    await asyncio.wait_for(q.drain(), timeout=2)
    await q.stop()


async def _test_processed_sentry_is_bounded_impl():
    """processed 观察哨有上限裁剪（防生产实例无限增长）。"""
    from src.im.engine.media import PROCESSED_MAX

    async def describe(ref, ctx):
        return "d"

    async def on_result(*a):
        pass

    q = MediaWorkQueue(describe, on_result, concurrency=4)
    await q.start()
    q.enqueue("i1", "oc1", [
        MediaRef(type=MsgType.IMAGE, storage_key=f"img_{i}") for i in range(PROCESSED_MAX + 50)
    ])
    await asyncio.wait_for(q.drain(), timeout=30)
    await q.stop()
    assert len(q.processed) <= PROCESSED_MAX


def test_enqueue_does_not_block_pipeline():
    asyncio.run(_test_enqueue_does_not_block_pipeline_impl())

def test_worker_describes_and_delivers_result():
    asyncio.run(_test_worker_describes_and_delivers_result_impl())

def test_worker_failure_isolated():
    asyncio.run(_test_worker_failure_isolated_impl())

def test_router_enqueues_to_queue():
    asyncio.run(_test_router_enqueues_to_queue_impl())

def test_worker_cancel_calls_task_done_once():
    asyncio.run(_test_worker_cancel_calls_task_done_once_impl())

def test_processed_sentry_is_bounded():
    asyncio.run(_test_processed_sentry_is_bounded_impl())
