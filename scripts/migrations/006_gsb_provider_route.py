"""迁移 006_gsb_provider_route（2026-11-14，Issue #2479）—— GSB provider 路由配置表.

背景：Issue #2479 把 GSB provider 从 ``GRAPE_PROVIDER_MODE`` 全局一刀切改为
按 ``service.method`` / ``domain.primitive`` 两级 key 路由，配置落
``gsb_provider_route`` 表（与 ``gsb_service_registry`` 同库，复用注册表连接），
进程内 version+TTL 缓存（默认 30s，``GRAPE_GSB_ROUTE_TTL``），改表不重启生效。

本迁移只做一件事（幂等）：建 ``gsb_provider_route`` 表。
运行时路径（src/kernel/gsb/route_config.py::ensure_table）同样幂等建表——
本脚本用于显式纳管 / 预建（与 001~005 的台账口径一致）。

用法：
    cd <repo-root>
    python3 scripts/migrations/006_gsb_provider_route.py --dry-run
    python3 scripts/migrations/006_gsb_provider_route.py
"""
from __future__ import annotations

import logging
import os
import sys

_log = logging.getLogger(__name__)

_MIGRATION_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_MIGRATION_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_DDL = """
CREATE TABLE IF NOT EXISTS gsb_provider_route (
    route_key  TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    timeout_ms INTEGER DEFAULT 1000,
    version    INTEGER DEFAULT 1,
    updated_at REAL DEFAULT 0,
    updated_by TEXT DEFAULT ''
)
"""


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="gsb_provider_route table migration")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=os.environ.get("DEVOPS_DB_PATH", ""))
    args = parser.parse_args()

    if args.db:
        os.environ["DEVOPS_DB_PATH"] = args.db
    from src.kernel import session_manager
    from src.kernel.gsb import route_config

    if args.dry_run:
        print("[006] dry-run: would CREATE TABLE IF NOT EXISTS gsb_provider_route "
              f"-> {os.environ.get('DEVOPS_DB_PATH', '<session default>')}")
        return 0

    # 表已存在也幂等通过；并验证 DDL 可执行
    route_config.ensure_table()
    rows = route_config.list_routes()
    print(f"[006] gsb_provider_route ready (existing rows: {len(rows)})")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
