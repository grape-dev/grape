"""LocalStorage — 技能对象本地磁盘后端（OBS 未启用时单机可用）。

见 docs/weekly-06/skill-local-storage-fallback-design.md：
- 权威存储落盘 `SKILL_STORAGE_DIR`（默认 <部署根>/data/skills/）；
- key 与 `skill_key(...)` 完全一致 → 本地路径 = `{root}/{object_key}`（含 OBS_PREFIX 段）；
- 仅单机语义（无多实例共享需求）；集群应 `SKILL_STORAGE=obs` 或共享盘。
"""
import os
import shutil

from src.config import log
from src.storage import StorageBackend


class LocalStorage(StorageBackend):
    """磁盘对象存储后端：key → {root}/{key}。"""

    def __init__(self, root: str):
        self._root = os.path.abspath(root)

    def backend_name(self) -> str:
        return "local"

    def _path(self, key: str) -> str:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"invalid local key: {key}")
        return os.path.join(self._root, *key.split("/"))

    def save_bytes(self, key: str, data: bytes) -> str:
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        _tmp = p + ".tmp"
        with open(_tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, p)
        return key

    def save_stream(self, key: str, source: str) -> str:
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        shutil.copyfile(source, p)
        return key

    def read_bytes(self, key: str) -> bytes | None:
        p = self._path(key)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "rb") as f:
                return f.read()
        except Exception as e:
            log.warning(f"LocalStorage read failed {key}: {e}")
            return None

    def read_file(self, key: str, local_path: str) -> bool:
        data = self.read_bytes(key)
        if data is None:
            return False
        _dir = os.path.dirname(local_path)
        if _dir:
            os.makedirs(_dir, exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        return True

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix) if prefix else self._root
        out: list[str] = []
        if not os.path.isdir(base):
            return out
        for root, _dirs, files in os.walk(base):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), self._root)
                out.append(rel.replace(os.sep, "/"))
        return out

    def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix) if prefix else self._root
        if not os.path.isdir(base):
            return 0
        count = 0
        for _r, _d, files in os.walk(base):
            count += len(files)
        shutil.rmtree(base, ignore_errors=True)
        return count

    def delete_object(self, key: str) -> bool:
        p = self._path(key)
        try:
            if os.path.isfile(p):
                os.remove(p)
                return True
        except Exception as e:
            log.warning(f"LocalStorage delete failed {key}: {e}")
        return False

    def copy_object(self, src_key: str, dst_key: str) -> bool:
        data = self.read_bytes(src_key)
        if data is None:
            return False
        try:
            self.save_bytes(dst_key, data)
            return True
        except Exception as e:
            log.warning(f"LocalStorage copy failed {src_key}->{dst_key}: {e}")
            return False

    def temporary_url(self, key: str, expires: int = 300) -> str:
        return ""

    def list_objects_with_meta(self, prefix: str, cursor: str = "", limit: int = 1000) -> tuple[list[dict], str | None]:
        keys = self.list_keys(prefix)
        items = [{"key": k, "size": os.path.getsize(self._path(k)) if self.exists(k) else 0,
                  "last_modified": ""} for k in keys]
        return items, None