"""迁移 005_outbox_purge_index（2026-11-13）—— outbox 保留期清理所需索引。

背景：outbox（grape_t_channel_outbound_card）只标记终态、从不删除 → 万人级折算
30 天 1.8M / 90 天 5.4M 行；`/api/im/status` 的未送达明细原先 `status<>'sent'` 全表扫，
200k 行 22ms、30 天后外推 199ms。

本迁移只做一件事（幂等）：建 (status, updated_at) 复合索引，供两处查询使用：
  - recent_undelivered：status IN ('pending','sending','failed','expired') ORDER BY updated_at DESC
  - purge_old_rows：status IN ('sent','failed','expired') AND updated_at < cutoff ORDER BY updated_at

用法：
    cd <repo-root>
    .venv/bin/python scripts/migrations/005_outbox_purge_index.py --dry-run
    .venv/bin/python scripts/migrations/005_outbox_purge_index.py
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys

_log = logging.getLogger(__name__)

_MIGRATION_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_MIGRATION_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TABLE = "grape_t_channel_outbound_card"
_IDX = "idx_outbound_card_status_updated"


def ensure_purge_index(conn) -> bool:
    """建 (status, updated_at) 索引（幂等）。返回是否本次新建。

    SQLite：CREATE INDEX IF NOT EXISTS；MySQL：information_schema 探测后直发
    （MySQL 不支持 IF NOT EXISTS——与 004 的 _ensure_status_index 同款处理）。
    失败只 warning 不阻断启动（索引缺失只影响性能，不影响功能）。
    """
    from src.kernel.db_health import is_sqlite_conn  # 自愈代理接入后 SQLite 判别统一走它
    if is_sqlite_conn(conn):
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {_IDX} ON {_TABLE} (status, updated_at)")
            conn.commit()
            return True
        except Exception as e:
            _log.warning("[005] ensure purge index failed (sqlite): %s", e)
            return False
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? "
            f"AND INDEX_NAME='{_IDX}'", (_TABLE,)).fetchone()
        if row and row[0]:
            return False
        conn.execute(f"CREATE INDEX {_IDX} ON {_TABLE} (status, updated_at)")
        conn.commit()
        return True
    except Exception as e:
        _log.warning("[005] ensure purge index failed (mysql): %s", e)
        return False


def main() -> int:
    import argparse
    import importlib.util

    parser = argparse.ArgumentParser(description="outbox purge index migration")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=os.environ.get("DEVOPS_DB_PATH", ""))
    args = parser.parse_args()

    sys.path.insert(0, _REPO_ROOT)
    from src.domains.runtime.im.engine_wiring import _connect, default_db_path

    db_path = args.db or default_db_path()
    conn = _connect(db_path)
    try:
        if args.dry_run:
            print(f"[005] dry-run: would CREATE INDEX IF NOT EXISTS {_IDX} "
                  f"ON {_TABLE} (status, updated_at) -> {db_path}")
            return 0
        created = ensure_purge_index(conn)
        print(f"[005] index {_IDX}: {'created' if created else 'already present'}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())