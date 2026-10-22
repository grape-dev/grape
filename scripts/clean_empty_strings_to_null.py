#!/usr/bin/env python3
"""FR-11.4: unified_user_mapping 空串列清洗脚本。

将 gitcode_user / github_login / feishu_open_id / feishu_name / gitcode_nickname
列中的空串 '' 更新为 NULL，消除空串命中面。

幂等：UPDATE ... SET col=NULL WHERE col=''——重复执行无变化。
dry-run 模式先统计不修改。

用法：
  python scripts/clean_empty_strings_to_null.py --dry-run   # 只统计
  python scripts/clean_empty_strings_to_null.py              # 执行清洗
"""
import argparse
import sqlite3
import sys
import os

COLUMNS = ["gitcode_user", "github_login", "feishu_open_id", "feishu_name", "gitcode_nickname"]


def clean(db_path: str, dry_run: bool = False) -> int:
    if not os.path.exists(db_path):
        print(f"ERROR: database file not found: {db_path}")
        return 1
    db = sqlite3.connect(db_path)
    c = db.cursor()

    # 检查表是否存在
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='unified_user_mapping'")
    if not c.fetchone():
        print("ERROR: unified_user_mapping table not found")
        db.close()
        return 1

    total_cleaned = 0
    for col in COLUMNS:
        # 检查列是否存在
        c.execute(f"PRAGMA table_info(unified_user_mapping)")
        cols = [r[1] for r in c.fetchall()]
        if col not in cols:
            print(f"  SKIP: column '{col}' not found in table")
            continue

        # 统计空串行
        c.execute(f"SELECT COUNT(*) FROM unified_user_mapping WHERE {col}=''")
        count = c.fetchone()[0]
        if count == 0:
            print(f"  {col}: 0 empty-string rows (clean)")
            continue

        if dry_run:
            print(f"  {col}: {count} empty-string rows (dry-run, not cleaning)")
        else:
            c.execute(f"UPDATE unified_user_mapping SET {col}=NULL WHERE {col}=''")
            print(f"  {col}: cleaned {count} empty-string rows → NULL")
            total_cleaned += count

    if not dry_run:
        db.commit()
        print(f"\nDone: {total_cleaned} rows cleaned.")
    else:
        print(f"\nDry-run complete. Run without --dry-run to execute.")

    db.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Clean empty strings to NULL in unified_user_mapping")
    parser.add_argument("--dry-run", action="store_true", help="Only count, don't modify")
    parser.add_argument("--db", default="sessions.db", help="Database file path (default: sessions.db)")
    args = parser.parse_args()
    sys.exit(clean(args.db, args.dry_run))


if __name__ == "__main__":
    main()
