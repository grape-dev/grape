"""Issue #3742：分布式锁契约（消费 acquired / strict 模式 / TTL 单点化）。

症状：`run_with_distributed_lock` 未抢到锁时 `yield False`，但 5 处调用点都不接收
yielded 值，导致 4 实例部署下每周期各自跑一遍同步/清理；`skill_sync` 还出现
「前半段全量同步 4 遍、场景同步被跳过」与 58 条/日 ERROR 噪音。

这里锁住契约：
- 未抢到锁：非严格模式 yield False 不抛；strict=True 抛 LockNotAcquired。
- 两个 worker 并发进入 `_skill_sync_worker`，`sync_official_skills` 只执行 1 次。
- 5 处调用点均显式 `as acquired` 且未持锁跳过主体。
- `SKILL_SYNC_LOCK_TTL` 单点派生（worker CM / `_ensure_outer_lock` / 超时阈值）。
"""
import asyncio
import inspect
import sys
import threading
import types
from pathlib import Path

import pytest

from src.domains.runtime.storage import skill_sync as ss

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeDistributedLock:
    """单持有者语义的进程内锁桩（acquire 在 to_thread 中执行，需线程安全）。"""

    def __init__(self, holder=None):
        self._guard = threading.Lock()
        self._holder = holder
        self.acquisitions = 0
        self.releases = 0

    def acquire_lock(self, key, ttl=300):
        with self._guard:
            if self._holder is None:
                self._holder = key
                self.acquisitions += 1
                return True
            return False

    def renew_lock(self, key, ttl=300):
        with self._guard:
            return self._holder == key

    def release_lock(self, key):
        with self._guard:
            if self._holder == key:
                self._holder = None
                self.releases += 1
            return True

    def cleanup_expired_locks(self):
        return 0


def _install_lock_stub(monkeypatch, fake):
    stub = types.ModuleType("src.kernel.lock")
    stub.acquire_lock = fake.acquire_lock
    stub.renew_lock = fake.renew_lock
    stub.release_lock = fake.release_lock
    stub.cleanup_expired_locks = fake.cleanup_expired_locks
    monkeypatch.setitem(sys.modules, "src.kernel.lock", stub)
    return stub


def test_strict_mode_raises_when_lock_held(monkeypatch):
    from src.domains.runtime.engine.task_lock import (
        LockNotAcquired,
        run_with_distributed_lock,
    )

    fake = _FakeDistributedLock(holder="skill_sync")
    _install_lock_stub(monkeypatch, fake)

    async def _run():
        async with run_with_distributed_lock("skill_sync", strict=True):
            raise AssertionError("body must not run when lock is held")

    with pytest.raises(LockNotAcquired):
        asyncio.run(_run())


def test_non_strict_mode_yields_false_without_raising(monkeypatch):
    from src.domains.runtime.engine.task_lock import run_with_distributed_lock

    fake = _FakeDistributedLock(holder="skill_sync")
    _install_lock_stub(monkeypatch, fake)

    seen = []

    async def _run():
        async with run_with_distributed_lock("skill_sync") as acquired:
            seen.append(acquired)

    asyncio.run(_run())
    assert seen == [False]


def test_two_workers_concurrently_call_sync_once(monkeypatch):
    """两个 worker 同时进入 `_skill_sync_worker`，同步主体只执行 1 次。"""
    calls = []

    async def _fake_sync():
        calls.append(1)
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(ss, "sync_official_skills", _fake_sync)
    fake = _FakeDistributedLock()
    _install_lock_stub(monkeypatch, fake)

    async def _run():
        workers = [
            asyncio.create_task(ss._skill_sync_worker(interval=0.01)),
            asyncio.create_task(ss._skill_sync_worker(interval=0.01)),
        ]
        await asyncio.sleep(0.2)
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    asyncio.run(_run())
    assert len(calls) == 1, f"sync must run once, got {len(calls)}"


def test_all_call_sites_consume_acquired():
    files = [
        "src/domains/runtime/storage/skill_sync.py",
        "src/api/main.py",
        "src/api/grape_server.py",
        "src/domains/runtime/storage/upload_cleanup.py",
    ]
    pattern = "run_with_distributed_lock("
    total = 0
    for rel in files:
        content = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for line in content.splitlines():
            if pattern in line and not line.lstrip().startswith("#"):
                total += int("as acquired" in line)
    assert total == 5, f"expected 5 call sites consuming acquired, got {total}"


def test_skill_sync_ttl_single_source():
    assert ss.SKILL_SYNC_LOCK_TTL == 600
    worker_src = inspect.getsource(ss._skill_sync_worker)
    assert "ttl=SKILL_SYNC_LOCK_TTL" in worker_src
    default_ttl = inspect.signature(ss._ensure_outer_lock).parameters["ttl"].default
    assert default_ttl == ss.SKILL_SYNC_LOCK_TTL
    cycle_src = inspect.getsource(ss.sync_official_skills)
    assert "SKILL_SYNC_LOCK_TTL" in cycle_src
    assert "900" not in cycle_src, "cycle threshold must derive from TTL, not hardcode 900"