#!/usr/bin/env python3
"""Schema 迁移脚本（Issue #699 Phase 2）。

把现有单库/单 schema 的 85 张注册表迁移到三域分 Schema：
- MySQL：CREATE SCHEMA IF NOT EXISTS grape_{mgmt,runtime,insight} + RENAME TABLE（跨 schema）。
- SQLite：ALTER TABLE ... RENAME TO 前缀表名（mgmt_/rt_/ins_，保留数据）。

幂等；--dry-run 只输出迁移计划。迁移记录写入 grape_t_schema_migrations 台账。

用法：
  python scripts/migrate_schemas.py                     # 按当前后端执行
  python scripts/migrate_schemas.py --backend sqlite    # 指定后端
  python scripts/migrate_schemas.py --dry-run           # 只看计划
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.kernel.db import (  # noqa: E402
    TABLE_REGISTRY,
    migrate_mysql_schemas,
    migrate_sqlite_prefixes,
)


def open_sqlite(path: str):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def open_mysql():
    import pymysql
    from src.kernel.mysql_compat import mysql_config
    cfg = mysql_config()
    database = cfg.pop("database", None)
    return pymysql.connect(**cfg, autocommit=False), database


def main() -> int:
    ap = argparse.ArgumentParser(description="Issue #699 Phase 2 schema migration")
    ap.add_argument("--backend", choices=["sqlite", "mysql"], default=None)
    ap.add_argument("--db-path", default=None, help="SQLite DB path (default DEVOPS_DB_PATH)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    backend = args.backend or os.environ.get("DEVOPS_DB_BACKEND", "sqlite").lower()
    if backend in ("mysql", "rds"):
        conn, _ = open_mysql()
        plan = migrate_mysql_schemas(conn, dry_run=args.dry_run)
    else:
        path = args.db_path or os.environ.get(
            "DEVOPS_DB_PATH",
            os.path.join(os.path.dirname(__file__), "..", "data", "sessions.db"),
        )
        conn = open_sqlite(path)
        plan = migrate_sqlite_prefixes(conn, dry_run=args.dry_run)

    if not plan:
        print(f"[{backend}] nothing to migrate ({len(TABLE_REGISTRY)} tables registered)")
        return 0
    for old, new in plan:
        print(f"[{backend}] {'DRY-RUN ' if args.dry_run else ''}{old} -> {new}")
    print(f"[{backend}] total: {len(plan)} table(s){' (dry-run, not executed)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
