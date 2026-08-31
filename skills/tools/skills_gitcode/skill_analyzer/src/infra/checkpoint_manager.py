from pathlib import Path
from typing import List, Optional

from src.infra.file_utils import ensure_dir


class CheckpointManager:
    def __init__(self, checkpoint_dir: str = "./cache/checkpoints"):
        self.checkpoint_dir = Path(checkpoint_dir)

    def get_db_path(self) -> Path:
        ensure_dir(self.checkpoint_dir)
        return self.checkpoint_dir / "checkpoints.db"

    def get_saver(self, use_memory: bool = False):
        if use_memory:
            from langgraph.checkpoint.memory import MemorySaver
            return MemorySaver()
        else:
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver
                db_path = str(self.get_db_path())
                return SqliteSaver.from_conn_string(db_path)
            except ImportError:
                from langgraph.checkpoint.memory import MemorySaver
                return MemorySaver()

    def list_checkpoints(self) -> List[dict]:
        db_path = self.get_db_path()
        if not db_path.exists():
            return []
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "SELECT thread_id, checkpoint FROM checkpoints ORDER BY thread_id"
            )
            results = []
            for row in cursor.fetchall():
                results.append({"thread_id": row[0], "checkpoint_id": row[1]})
            return results
        except Exception:
            return []
        finally:
            conn.close()

    def cleanup_expired(self, max_age_days: int = 7) -> int:
        from src.infra.cache_manager import CacheManager
        cm = CacheManager(str(self.checkpoint_dir.parent))
        return cm.cleanup_expired("checkpoints", max_age_days)