"""Issue #4295：派生通知任务不得继承前台 db_lock 等锁预算。

第 2 轮复审 Required 的现场：``asyncio.Task`` 在创建时**复制当前 ContextVars**，而
``src/api/server_common.py`` 对所有 HTTP 请求设置 ``path_var``。于是
``_fire_notify_coro`` 从请求线程派生的通知任务继承了请求上下文 →
``acquire_budget_s()`` 给它的 db_lock 等锁套上 ``DB_LOCK_ACQUIRE_TIMEOUT_S``（默认 5s）。

后果：审核点通知的投递本身要做多次 DB 往返（目标解析 / conv 兜底建 / 本人 bot 凭证 /
写 outbox），拥塞窗口里超过预算就抛 ``LockAcquireTimeout``；此时响应早已发出（进不了
503 处理器）、任务又无人回收异常，只剩一行 "Task exception was never retrieved" ——
**审核点通知静默丢失**。而本 PR 把通知移出 ``with db_lock`` 后，它从「同线程重入」变成
「一个新的等锁者」，所以这条失败路径是本 PR 新增的。

修复：载荷统一包进 ``_notify_without_foreground_budget``（任务内
``foreground_lock_budget(0)``），三条派发路径都覆盖。
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from src.domains.runtime import issues_center as ic
from src.kernel import db_lock_metrics as dlm
from src.kernel.config import path_var, request_id_var

_REQUEST_PATH = "/api/issues/acme/demo"


class _FakeLog:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, msg, *args, **kwargs) -> None:
        self.messages.append(msg % args if args else str(msg))

    def error(self, msg, *args, **kwargs):  # noqa: A003 - 模拟 logger.error
        self._record(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):  # noqa: A003
        self._record(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):  # noqa: A003
        self._record(msg, *args, **kwargs)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    dlm._BUDGET_OVERRIDE.set(None)
    with dlm._TIMEOUT_REPORTED_LOCK:
        dlm._TIMEOUT_REPORTED.clear()
    ic._NOTIFY_TASKS.clear()
    path_var.set("")
    request_id_var.set("-")
    yield
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    dlm._BUDGET_OVERRIDE.set(None)
    with dlm._TIMEOUT_REPORTED_LOCK:
        dlm._TIMEOUT_REPORTED.clear()
    ic._NOTIFY_TASKS.clear()
    path_var.set("")
    request_id_var.set("-")


def _hold_global_db_lock(hold_s: float):
    """后台线程持**进程级全局 db_lock** hold_s 秒（通知任务要等的那把锁）。"""
    from src.kernel.session_manager import db_lock

    ready = threading.Event()
    release = threading.Event()

    def _holder():
        with db_lock:
            ready.set()
            release.wait(hold_s)

    t = threading.Thread(target=_holder, name="bg-holder-4295", daemon=True)
    t.start()
    assert ready.wait(5), "holder failed to take db_lock"
    return t, release


def _releasing_holder(hold_s: float):
    """同上的确定性版本：hold_s 秒后**主动释放**（不依赖外层 finally 才放手）。"""
    from src.kernel.session_manager import db_lock

    ready = threading.Event()

    def _holder():
        with db_lock:
            ready.set()
            threading.Event().wait(hold_s)

    t = threading.Thread(target=_holder, name="bg-holder-4295", daemon=True)
    t.start()
    assert ready.wait(5), "holder failed to take db_lock"
    return t


async def _notify_payload(delivered: list, hold_span_s: float = 0.0):
    """通知载荷：像 _run_pending_review_notify 一样在 db_lock 内做 DB 往返。"""
    from src.kernel.session_manager import db_lock

    with db_lock:
        if hold_span_s:
            await asyncio.sleep(hold_span_s)
        delivered.append("delivered")


def test_derived_notify_task_outlives_the_foreground_budget(monkeypatch):
    """请求上下文 + 他人持锁超预算 → 派生通知任务仍完成投递（复审 Required 的验收判据）。"""
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.05")
    holder = _releasing_holder(0.4)
    delivered: list[str] = []
    try:
        async def _run():
            path_var.set(_REQUEST_PATH)  # 前台请求上下文（生产由中间件写入）
            ic._fire_notify_coro(_notify_payload(delivered))
            for _ in range(60):  # 最多等 1.2s
                if delivered:
                    break
                await asyncio.sleep(0.02)

        asyncio.run(_run())
    finally:
        holder.join(5)

    assert delivered == ["delivered"], (
        "派生通知任务必须在等锁超预算后继续等待并完成投递（否则审核点通知静默丢失）")
    assert dlm.snapshot() == [], "投递完成后不得留下持锁状态"
    assert ic._NOTIFY_TASKS == set(), "收尾回调必须解除强引用（不留悬挂引用）"


def test_derived_notify_task_from_sync_thread_also_ignores_budget(monkeypatch):
    """无事件循环（worker 线程/同步上下文）的派发路径同样不得继承预算。"""
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.05")
    holder = _releasing_holder(0.4)
    delivered: list[str] = []
    path_var.set(_REQUEST_PATH)
    try:
        # 无运行中的 loop → 走 run_coroutine_threadsafe / asyncio.run 兜底路径
        ic._fire_notify_coro(_notify_payload(delivered))
    finally:
        holder.join(5)

    assert delivered == ["delivered"], (
        "同步派发路径也必须关掉前台预算（否则 503 之外的静默丢失仍存在）")


def test_without_isolation_the_same_derived_task_dies_on_the_budget(monkeypatch):
    """反证：把包装去掉（裸 create_task）时同一场景必定失败——隔离层是承重的。

    这条用例保证上一条断言不会因为「预算根本没生效」而空转通过。
    """
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "0.05")
    holder = _releasing_holder(0.4)
    delivered: list[str] = []
    try:
        async def _run():
            path_var.set(_REQUEST_PATH)
            task = asyncio.get_running_loop().create_task(_notify_payload(delivered))
            await asyncio.sleep(0.2)  # 越过 0.05s 预算
            return task

        task = asyncio.run(_run())
    finally:
        holder.join(5)

    assert delivered == [], "裸派生任务必须在预算耗尽时终止（这正是修复前的现场）"
    assert task.done()
    assert isinstance(task.exception(), dlm.LockAcquireTimeout), (
        f"未隔离的派生任务应抛 LockAcquireTimeout，实际 {task.exception()!r}")


def test_failed_notify_task_is_logged_and_not_retained(monkeypatch):
    """任务异常必须显式记日志并解除强引用（不再只剩 never-retrieved）。"""
    fake = _FakeLog()
    monkeypatch.setattr(ic, "log", fake)

    async def _boom():
        raise RuntimeError("notify exploded")

    async def _run():
        ic._fire_notify_coro(_boom())
        for _ in range(50):
            if any("background notify failed" in m for m in fake.messages):
                break
            await asyncio.sleep(0.02)

    asyncio.run(_run())

    assert any("background notify failed" in m and "notify exploded" in m
               for m in fake.messages), f"失败通知必须留下日志，实际 {fake.messages}"
    assert ic._NOTIFY_TASKS == set(), "失败任务同样要移出强引用集合"


def test_notify_wrapper_disables_budget_without_leaking_to_caller(monkeypatch):
    """包装只在任务自己的 context 里生效：调用方/事件循环的预算保持继承值。"""
    monkeypatch.setenv(dlm.ACQUIRE_TIMEOUT_ENV, "5")
    path_var.set(_REQUEST_PATH)
    assert dlm.acquire_budget_s() == pytest.approx(5.0)

    inside: list[float] = []

    async def _probe():
        inside.append(dlm.acquire_budget_s())

    async def _run():
        path_var.set(_REQUEST_PATH)
        assert dlm.acquire_budget_s() == pytest.approx(5.0), "派发前是前台请求预算"
        task = asyncio.get_running_loop().create_task(
            ic._notify_without_foreground_budget(_probe()))
        await asyncio.wait_for(task, timeout=5)
        assert dlm.acquire_budget_s() == pytest.approx(5.0), (
            "派生任务的预算覆盖不得污染事件循环所在上下文")

    asyncio.run(_run())
    assert inside == [0.0], f"包装内预算必须关闭（0=关闭），实际 {inside}"
    assert dlm.acquire_budget_s() == pytest.approx(5.0), "包装不得污染调用方上下文"