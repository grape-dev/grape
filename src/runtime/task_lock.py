"""Scheduled task distributed lock helper (P1-D, Issue #357).

When uvicorn runs with --workers N, each worker starts its own asyncio scheduler.
Without coordination, cleanup/sync tasks execute N times concurrently, causing:
- Duplicate DB writes / deadlocks
- Redundant external API calls
- Race conditions on shared state

This module provides ``run_with_distributed_lock`` — a context manager that
uses the existing ``distributed_locks`` table (src/lock.py) to ensure only one
worker executes a given task at a time.

Usage in scheduler workers::

    from src.runtime.task_lock import run_with_distributed_lock

    async def _my_worker(interval=300):
        while True:
            async with run_with_distributed_lock("my_task", ttl=600):
                do_work()
            await asyncio.sleep(interval)

If the lock is not acquired (another worker holds it), the body is skipped
silently — the other worker is already doing the work.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from src.config import log

logger = logging.getLogger(__name__)


@asynccontextmanager
async def run_with_distributed_lock(lock_key: str, ttl: int = 300):
    """Acquire a distributed lock before running the task body.

    Runs ``acquire_lock`` in a thread (it does blocking DB ops under db_lock).
    If the lock is not acquired, the body is skipped (another worker has it).

    Args:
        lock_key: unique key for this task (e.g. "scene_sync", "upload_cleanup")
        ttl: lock TTL in seconds (auto-expires if holder crashes)

    Yields:
        bool: True if lock was acquired, False if another worker holds it.
    """
    from src.lock import acquire_lock, release_lock

    acquired = await asyncio.to_thread(acquire_lock, lock_key, ttl)
    if not acquired:
        logger.debug(f"task_lock: {lock_key} held by another worker, skipping")
        yield False
        return
    try:
        yield True
    finally:
        try:
            await asyncio.to_thread(release_lock, lock_key)
        except Exception as e:
            log.warning(f"task_lock: release {lock_key} failed: {e}")
