"""skill_sync 调度器幂等 + 外层锁续期重抢（回归）。

两个症状同源，都出在「同一进程里跑了不止一个 skill-sync 调度器」：

1. `start_skill_sync_scheduler()` 被三个入口调用（`src/api/main.py`、
   `src/api/server_tasks.py`、`src/api/grape_server.py`），而它此前每次调用都新建一个
   asyncio 任务。同进程两个 worker 共用模块级的锁 holder，A 的 release 会按 holder
   删掉 B 刚拿到的锁行。
2. 于是 B 在下一轮续期必然失败，日志每轮一条
   `outer lock lost before scene step`，并且场景同步步骤被跳过——场上其实只有一个
   存活进程。

这里锁住三件事：重复 start 只起一个任务；续期成功不碰 acquire；续期失败但可重新抢到时
继续本轮（只 WARNING）；两边都失败才 ERROR 返回 False。
"""
import asyncio
import logging
import sys
import types

import pytest

from src.domains.runtime.storage import skill_sync as ss


@pytest.fixture(autouse=True)
def _clean_scheduler_state(monkeypatch):
    monkeypatch.setattr(ss, "_scheduler_task", None)
    ss._bg_tasks.clear()
    yield
    for task in list(ss._bg_tasks):
        task.cancel()
    ss._bg_tasks.clear()


def test_start_scheduler_is_idempotent(monkeypatch):
    started = []

    async def _fake_worker(interval):
        started.append(interval)
        await asyncio.sleep(3600)

    monkeypatch.setattr(ss, "_skill_sync_worker", _fake_worker)

    async def _run():
        ss.start_skill_sync_scheduler()
        ss.start_skill_sync_scheduler()
        ss.start_skill_sync_scheduler()
        await asyncio.sleep(0)
        return [t for t in ss._bg_tasks if not t.done()]

    running = asyncio.run(_run())
    assert len(running) == 1, running
    assert len(started) == 1, started


def _install_lock_stub(monkeypatch, renew_ok, acquire_ok):
    """替换 src.kernel.lock，记录调用次数（helper 在函数内 import，故走 sys.modules）。"""
    calls = {"renew": 0, "acquire": 0}

    def _renew(key, ttl):
        calls["renew"] += 1
        return renew_ok

    def _acquire(key, ttl):
        calls["acquire"] += 1
        return acquire_ok

    stub = types.ModuleType("src.kernel.lock")
    stub.renew_lock = _renew
    stub.acquire_lock = _acquire
    stub.release_lock = lambda *a, **k: True
    monkeypatch.setitem(sys.modules, "src.kernel.lock", stub)
    return calls


def test_outer_lock_renewed_without_reacquire(monkeypatch):
    calls = _install_lock_stub(monkeypatch, renew_ok=True, acquire_ok=True)
    assert asyncio.run(ss._ensure_outer_lock()) is True
    assert calls == {"renew": 1, "acquire": 0}


def test_outer_lock_reacquired_when_row_was_dropped(monkeypatch, caplog):
    calls = _install_lock_stub(monkeypatch, renew_ok=False, acquire_ok=True)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(ss._ensure_outer_lock()) is True
    assert calls == {"renew": 1, "acquire": 1}
    assert any("re-acquired" in r.message for r in caplog.records), [r.message for r in caplog.records]


def test_outer_lock_lost_when_taken_over(monkeypatch, caplog):
    calls = _install_lock_stub(monkeypatch, renew_ok=False, acquire_ok=False)
    with caplog.at_level(logging.INFO):
        assert asyncio.run(ss._ensure_outer_lock()) is False
    assert calls == {"renew": 1, "acquire": 1}
    lost = [r for r in caplog.records if "outer lock lost" in r.message]
    assert lost, [r.message for r in caplog.records]
    assert lost[0].levelno == logging.INFO, lost[0].levelname


def test_outer_lock_error_is_logged_as_error(monkeypatch, caplog):
    """锁服务异常（renew/acquire 抛异常）→ ERROR 并跳过本轮。"""
    stub = types.ModuleType("src.kernel.lock")

    def _raise(key, ttl):
        raise RuntimeError("db down")

    stub.renew_lock = _raise
    stub.acquire_lock = _raise
    stub.release_lock = lambda *a, **k: True
    monkeypatch.setitem(sys.modules, "src.kernel.lock", stub)
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(ss._ensure_outer_lock()) is False
    errs = [r for r in caplog.records if "outer lock renew/re-acquire failed" in r.message]
    assert errs and errs[0].levelno == logging.ERROR
