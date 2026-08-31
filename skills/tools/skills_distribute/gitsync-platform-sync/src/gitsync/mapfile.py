"""Map file: append-only local cache mapping gitcode github-branch commits
to rewritten remote commits and master source commits.

Format (one entry per line, 3 columns, space-separated):
    <github_branch_commit> <remote_commit> <master_source_commit>

The map is a cache, not the source of truth — it can be rebuilt via
`map rebuild`. Writes are atomic (temp + rename) AND serialized with an
advisory file lock (fcntl.flock) so concurrent processes appending entries
cannot lose each other's rows (2026-08-05: 10-process race lost 60%).
"""
from __future__ import annotations

import fcntl
import os
import tempfile
from typing import Dict, Optional


class MapFile:
    def __init__(self, path: str):
        self.path = path
        self._branch_to_remote: Dict[str, str] = {}
        self._remote_to_branch: Dict[str, str] = {}
        self._branch_to_master: Dict[str, str] = {}
        self._master_to_branch: Dict[str, str] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if os.path.isfile(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) != 3:
                        continue
                    branch, remote, master = parts
                    self._branch_to_remote[branch] = remote
                    self._remote_to_branch[remote] = branch
                    self._branch_to_master[branch] = master
                    self._master_to_branch[master] = branch
        self._loaded = True

    def branch_to_remote(self, sha: str) -> Optional[str]:
        self._load()
        return self._branch_to_remote.get(sha)

    def remote_to_branch(self, sha: str) -> Optional[str]:
        self._load()
        return self._remote_to_branch.get(sha)

    def branch_to_master(self, sha: str) -> Optional[str]:
        self._load()
        return self._branch_to_master.get(sha)

    def master_to_branch(self, sha: str) -> Optional[str]:
        self._load()
        return self._master_to_branch.get(sha)

    def has_branch(self, sha: str) -> bool:
        self._load()
        return sha in self._branch_to_remote

    def has_remote(self, sha: str) -> bool:
        self._load()
        return sha in self._remote_to_branch

    def has_master(self, sha: str) -> bool:
        self._load()
        return sha in self._master_to_branch

    def append(self, entries: list) -> None:
        """Append entries and atomically rewrite the file.

        Serialized with a lock file (fcntl.flock) so concurrent processes
        appending different entries don't lose rows (read-modify-write race).
        entries: list of (branch, remote, master) tuples.
        """
        if not entries:
            return
        lock_path = self.path + ".lock"
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                self._load()
                lines = []
                if os.path.isfile(self.path):
                    with open(self.path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                for branch, remote, master in entries:
                    lines.append(f"{branch} {remote} {master}\n")
                    self._branch_to_remote[branch] = remote
                    self._remote_to_branch[remote] = branch
                    self._branch_to_master[branch] = master
                    self._master_to_branch[master] = branch
                self._atomic_write(lines)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _atomic_write(self, lines: list) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".map.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
