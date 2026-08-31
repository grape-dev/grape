import shutil
from pathlib import Path
from typing import Optional

from src.infra.file_utils import ensure_dir


class CacheManager:
    def __init__(self, cache_root: str = "./cache"):
        self.cache_root = Path(cache_root)

    def get_cache_dir(self, name: str) -> Path:
        cache_dir = self.cache_root / name
        return ensure_dir(cache_dir)

    def is_cache_valid(self, name: str, marker_file: str = ".cache_valid") -> bool:
        cache_dir = self.cache_root / name
        return (cache_dir / marker_file).exists()

    def mark_cache_valid(self, name: str, marker_file: str = ".cache_valid") -> None:
        cache_dir = self.cache_root / name
        ensure_dir(cache_dir)
        (cache_dir / marker_file).write_text("valid", encoding="utf-8")

    def clear_cache(self, name: str) -> None:
        cache_dir = self.cache_root / name
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)

    def get_cache_size_mb(self, name: str) -> float:
        cache_dir = self.cache_root / name
        if not cache_dir.exists():
            return 0.0
        total_size = sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file())
        return total_size / (1024 * 1024)

    def cleanup_expired(self, name: str, max_age_days: int = 30) -> int:
        import time
        cache_dir = self.cache_root / name
        if not cache_dir.exists():
            return 0
        cutoff = time.time() - (max_age_days * 86400)
        removed = 0
        for f in cache_dir.rglob("*"):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        return removed