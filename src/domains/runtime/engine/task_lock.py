"""Scheduled task distributed lock helper (P1-D, Issue #357).

When uvicorn runs with --workers N, each worker starts its own asyncio scheduler.
Without coordination, cleanup/sync tasks execute N times concurrently, causing:
- Duplicate DB writes / deadlocks
- Redundant external API calls
- Race conditions on shared state

This module provides ``run_with_distributed_lock`` — a context manager that
uses the existing ``distributed_locks`` table (src/kernel/lock.py) to ensure only one
worker executes a given task at a time.

Usage in scheduler workers::

    from src.domains.runtime.engine.task_lock import run_with_distributed_lock

    async def _my_worker(interval=300):
        while True:
            async with run_with_distributed_lock("my_task", ttl=600) as acquired:
                # another worker may be doing the work — skip this cycle
                if acquired:
                    do_work()
            await asyncio.sleep(interval)

Contract (Issue #3742): this context manager does **NOT** skip the body for
you. When the lock is held by another worker it yields ``False``; the caller is
responsible for checking the yielded value and skipping its own work. Passing
``strict=True`` turns "not acquired" into a ``LockNotAcquired`` exception.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from src.kernel.config import log

logger = logging.getLogger(__name__)


class LockNotAcquired(RuntimeError):
    """Raised by ``run_with_distributed_lock(..., strict=True)`` when the lock is held."""


@asynccontextmanager
async def run_with_distributed_lock(lock_key: str, ttl: int = 300, *, strict: bool = False):
    """Acquire a distributed lock before running the task body.

    Runs ``acquire_lock`` in a thread (it does blocking DB ops under db_lock).
    The body is never skipped by this context manager: check the yielded value.

    Args:
        lock_key: unique key for this task (e.g. "scene_sync", "upload_cleanup")
        ttl: lock TTL in seconds (auto-expires if holder crashes)
        strict: when True, raise ``LockNotAcquired`` instead of yielding ``False``

    Yields:
        bool: True if lock was acquired, False if another worker holds it.
        With ``strict=True`` the False case raises instead of yielding.

    Raises:
        LockNotAcquired: only when ``strict=True`` and another worker holds the lock.
    """
    from src.kernel.lock import acquire_lock, release_lock

    acquired = await asyncio.to_thread(acquire_lock, lock_key, ttl)
    if not acquired:
        logger.debug(f"task_lock: {lock_key} held by another worker, skipping")
        if strict:
            raise LockNotAcquired(f"distributed lock {lock_key!r} is held by another worker")
        yield False
        return
    try:
        yield True
    finally:
        try:
            await asyncio.to_thread(release_lock, lock_key)
        except Exception as e:
            log.warning(f"task_lock: release {lock_key} failed: {e}")
