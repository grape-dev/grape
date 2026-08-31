"""Single-instance run lock for `gitsync run` (2026-08-16).

Multiple distribution tasks may arrive concurrently; they share map/manifest/
ClawHub version state, so runs must be serialized. The lock lives in Python
(run command entry) rather than only in scripts/agent-run.sh so that runs
spawned directly via `python3 -m gitsync.cli run` (bypassing agent-run.sh)
are also protected.

Semantics: queue-and-wait, not fail-fast — a waiting run blocks (polling
LOCK_NB) until the holder releases, so two concurrent tasks both succeed
sequentially. Only a wait longer than the configured budget fails (exit 2),
mirroring the old agent-run.sh `flock -w` behavior. The kernel releases the
lock automatically if the holder dies, so no stale-lock cleanup is needed.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from typing import Optional


class RunLockTimeout(Exception):
    """Timed out waiting for the run lock (another run holds it)."""


class RunLock:
    def __init__(self, lock_path: str, wait_sec: int = 600, poll: float = 0.5):
        self.lock_path = lock_path
        self.wait_sec = max(1, int(wait_sec))
        self.poll = poll
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        directory = os.path.dirname(self.lock_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.wait_sec
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(fd)
                    raise
            if time.monotonic() >= deadline:
                os.close(fd)
                raise RunLockTimeout(
                    f"timed out waiting {self.wait_sec}s for run lock "
                    f"(held by another run): {self.lock_path}"
                )
            time.sleep(self.poll)

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def lock_path_for_config(config) -> str:
    """Lock file next to the map file (shared runtime data dir)."""
    base = getattr(config, "map_file", "") or ""
    directory = os.path.dirname(base) or "."
    return os.path.join(directory, ".gitsync-run.lock")


def wait_sec_from_env(default: int = 1200) -> int:
    try:
        return max(1, int(os.environ.get("GITSYNC_LOCK_WAIT_SEC", str(default))))
    except ValueError:
        return default
