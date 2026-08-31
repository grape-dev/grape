"""ClawHub publish manifest: per-skill publish state machine.

The manifest is the only holder of in-review status (the public API cannot see
versions under review), so it is critical state with a redundant backup copy.
Writes are atomic AND serialized with an advisory file lock (fcntl.flock) so
concurrent publish runs cannot clobber each other's entries (2026-08-05).
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from typing import Dict, List, Optional


class Manifest:
    def __init__(self, path: str, backup_path: str = ""):
        self.path = path
        self.backup_path = backup_path
        self.data: Dict[str, dict] = {}

    def load(self) -> None:
        for p in (self.path, self.backup_path):
            if p and os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data = loaded
                    return
                except (json.JSONDecodeError, OSError):
                    continue

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        # Serialize concurrent saves (read-modify-write on self.data) so two
        # parallel publish runs can't overwrite each other's entries.
        lock_path = self.path + ".lock"
        with open(lock_path, "w", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                for target in (self.path, self.backup_path):
                    if not target:
                        continue
                    tdir = os.path.dirname(target) or "."
                    os.makedirs(tdir, exist_ok=True)
                    fd, tmp = tempfile.mkstemp(dir=tdir, prefix=".manifest.", suffix=".tmp")
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            json.dump(self.data, f, ensure_ascii=False, indent=2)
                        os.replace(tmp, target)
                    except BaseException:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                        raise
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def get_skill(self, name: str) -> Optional[dict]:
        return self.data.get(name)

    def set_skill(self, name: str, entry: dict) -> None:
        self.data[name] = entry

    def highest_version(self, name: str) -> Optional[str]:
        entry = self.data.get(name)
        if not entry:
            return None
        from .semver import highest
        versions = [v["version"] for v in entry.get("versions", [])]
        return highest(versions)

    def add_version(self, name: str, version: str, status: str,
                    content_hash: str = "") -> None:
        entry = self.data.setdefault(name, {"versions": []})
        entry["content_hash"] = content_hash
        entry.setdefault("versions", []).append(
            {"version": version, "status": status}
        )

    def content_hash(self, name: str) -> Optional[str]:
        entry = self.data.get(name)
        if not entry:
            return None
        return entry.get("content_hash")

    def versions(self, name: str) -> List[dict]:
        entry = self.data.get(name)
        return entry.get("versions", []) if entry else []
