"""一次性迁移 003：IM 渠道表统一 `grape_t_` 前缀（规则 2.0 A6）。

背景：渠道层 7 张表最初以 `channel_` 命名，不符合主控新表 `grape_t_xxx` 约定。
本迁移对**已有库**做原地改名（RENAME，数据不丢、索引随表走），新库由 001 直接建新名。

用法：

    cd <repo-root>
    .venv/bin/python scripts/migrations/003_channel_table_prefix.py --dry-run   # 只看计划
    .venv/bin/python scripts/migrations/003_channel_table_prefix.py             # 真执行

幂等：目标表已存在则跳过；旧表不存在也跳过（新库场景全跳过）。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

RENAMES = (
    ("channel_installation", "grape_t_channel_installation"),
    ("channel_chat_session_binding", "grape_t_channel_chat_session_binding"),
    ("channel_inbound_dedup", "grape_t_channel_inbound_dedup"),
    ("channel_inbound_message", "grape_t_channel_inbound_message"),
    ("channel_sender_identity", "grape_t_channel_sender_identity"),
    ("channel_outbound_card", "grape_t_channel_outbound_card"),
    ("channel_lease", "grape_t_channel_lease"),
)


def _exists(conn, table: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()
        return True
    except Exception:
        return False


def rename_tables(conn, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """(old, new, result) —— result ∈ renamed / skipped_missing / skipped_exists / failed。"""
    out: list[tuple[str, str, str]] = []
    for old, new in RENAMES:
        if not _exists(conn, old):
            out.append((old, new, "skipped_missing"))
            continue
        if _exists(conn, new):
            out.append((old, new, "skipped_exists"))
            continue
        if dry_run:
            out.append((old, new, "would_rename"))
            continue
        try:
            conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
            conn.commit()
            out.append((old, new, "renamed"))
        except Exception as e:
            out.append((old, new, f"failed: {e}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="rename IM channel tables to the grape_t_ prefix")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from src.im.engine_wiring import _connect, default_db_path
    from src.im.engine_wiring import _is_mysql_backend
    backend = "mysql" if _is_mysql_backend() else "sqlite"
    conn = _connect(default_db_path())
    rows = rename_tables(conn, dry_run=args.dry_run)
    conn.close()

    print(f"backend={backend} dry_run={args.dry_run}")
    for old, new, res in rows:
        print(f"  {res:16s} {old} -> {new}")
    failed = [r for r in rows if r[2].startswith("failed")]
    if failed:
        print(f"FAILED: {len(failed)} 项未完成")
        return 1
    print("done（已存在/不存在的项按幂等跳过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())