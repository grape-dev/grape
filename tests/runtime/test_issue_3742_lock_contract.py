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
import re
import sys
import threading
import types
from pathlib import Path

import pytest

from src.domains.runtime.storage import skill_sync as ss

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeDistributedLock:
    """单持有者语义的进程内锁桩（acquire 在 to_thread 中执行，需线程安全）。

    `renew_fail_from=n`：第 n 次及之后的 renew 返回 False（模拟锁被其它实例接管），
    用于验证「续期失败即中止本轮剩余步骤」。
    """

    def __init__(self, holder=None, renew_fail_from=None):
        self._guard = threading.Lock()
        self._holder = holder
        self.acquisitions = 0
        self.releases = 0
        self.renew_calls = 0
        self.renew_fail_from = renew_fail_from

    def acquire_lock(self, key, ttl=300):
        with self._guard:
            if self._holder is None:
                self._holder = key
                self.acquisitions += 1
                return True
            return False

    def renew_lock(self, key, ttl=300):
        with self._guard:
            self.renew_calls += 1
            if self.renew_fail_from is not None and self.renew_calls >= self.renew_fail_from:
                return False
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
    # TTL 允许 env 覆盖（SKILL_SYNC_LOCK_TTL），断言「默认值 + 同源派生」而非把 600
    # 写死，避免运行环境设置了该 env 时误报。
    assert isinstance(ss.SKILL_SYNC_LOCK_TTL, int) and ss.SKILL_SYNC_LOCK_TTL > 0
    module_src = Path(ss.__file__).read_text(encoding="utf-8")
    assert re.search(
        r'SKILL_SYNC_LOCK_TTL = int\(os\.environ\.get\("SKILL_SYNC_LOCK_TTL", "600"\)\)',
        module_src,
    ), "TTL must keep env-overridable default 600"
    worker_src = inspect.getsource(ss._skill_sync_worker)
    assert "ttl=SKILL_SYNC_LOCK_TTL" in worker_src
    default_ttl = inspect.signature(ss._ensure_outer_lock).parameters["ttl"].default
    assert default_ttl == ss.SKILL_SYNC_LOCK_TTL
    cycle_src = inspect.getsource(ss.sync_official_skills)
    assert "SKILL_SYNC_LOCK_TTL" in cycle_src
    assert "900" not in cycle_src, "cycle threshold must derive from TTL, not hardcode 900"


def _install_sync_stubs(monkeypatch, tmp_path):
    """把 `sync_official_skills` 的外部依赖收窄为纯内存桩（不联网、不写 DB）。"""
    from src.domains.runtime.storage import scene_sync

    async def _no_download(client, token):
        raise RuntimeError("offline in test")

    monkeypatch.setattr(scene_sync, "_download_repo_zip_with_retry", _no_download)
    monkeypatch.setattr(scene_sync, "_get_sync_token", lambda: "")
    monkeypatch.setattr(ss, "CONFIG_DIR", str(tmp_path))  # 无 skill-description.json → 跳过 DB 步骤

    calls = {"obs": 0, "scene": 0}

    async def _fake_obs():
        calls["obs"] += 1
        return {"added": 0, "updated": 0, "skipped": 0}

    async def _fake_scenes(repo_root=None):
        calls["scene"] += 1
        return {}

    monkeypatch.setattr(ss, "sync_official_skills_to_obs", _fake_obs)
    monkeypatch.setattr(scene_sync, "sync_official_scenes", _fake_scenes)
    return calls


def test_obs_step_aborts_when_outer_lock_renew_fails(monkeypatch, tmp_path, caplog):
    """续期失败（锁已被其它实例接管）→ 不上传 OBS、不跑场景步骤，日志留 INFO。"""
    import logging

    fake = _FakeDistributedLock(holder="skill_sync", renew_fail_from=2)
    _install_lock_stub(monkeypatch, fake)
    calls = _install_sync_stubs(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO):
        asyncio.run(ss.sync_official_skills())

    assert fake.renew_calls >= 2, "must renew the outer lock before OBS upload"
    assert calls["obs"] == 0, "OBS upload must be skipped once the outer lock is lost"
    assert calls["scene"] == 0, "scene step must be skipped once the outer lock is lost"
    assert any("outer lock" in r.message for r in caplog.records)


def test_obs_step_runs_while_outer_lock_held(monkeypatch, tmp_path):
    """续期成功时 OBS 步骤照常执行（防「一律中止」式假绿）。"""
    fake = _FakeDistributedLock(holder="skill_sync")
    _install_lock_stub(monkeypatch, fake)
    calls = _install_sync_stubs(monkeypatch, tmp_path)

    asyncio.run(ss.sync_official_skills())

    assert fake.renew_calls >= 2
    assert calls["obs"] == 1
