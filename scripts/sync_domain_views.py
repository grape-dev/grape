#!/usr/bin/env python3
"""同步领域库的跨库视图（幂等）。

背景：库按域拆分后，非 owner 角色通过跨库可更新视图访问他域基表
（`docs/weekly-09/domain-db-split.md`）。视图是**建视图时**的列快照，
所以基表新增列（如 2026-11-12 的 `agent_tasks.trace_context`）或新增表
（`grape_t_credential_usage_log` / `grape_t_system_credentials`）都必须同步
重建视图，否则 mgmt/insight 侧读该列会 1054、写新表会 1146。

用法：
  DEVOPS_DB_HOST=<db-host> DEVOPS_DB_USER=<user> DEVOPS_DB_PASSWORD=<password> \
    python3 scripts/sync_domain_views.py [--dry-run]

目标映射（owner 恒为 grape_runtime_db）：
  grape_mgmt_db / grape_insight_db  ← views for owner tables
"""
import argparse
import os
import sys

import pymysql

OWNER_DB = "grape_runtime_db"
TARGET_DBS = ("grape_mgmt_db", "grape_insight_db")
# owner 侧基表 → 需要在非 owner 库建/重建视图
SYNC_TABLES = (
    "agent_tasks",
    "grape_t_credential_usage_log",
    "grape_t_system_credentials",
)


def connect(host, user, password, db=None):
    return pymysql.connect(host=host, user=user, password=password, database=db,
                           charset="utf8mb4", autocommit=True)


def columns(conn, schema, table):
    with conn.cursor() as c:
        c.execute(
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (schema, table),
        )
        return [r[0] for r in c.fetchall()]


def table_type(conn, schema, table):
    with conn.cursor() as c:
        c.execute(
            "SELECT TABLE_TYPE FROM information_schema.tables "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (schema, table),
        )
        row = c.fetchone()
        return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    host = os.environ["DEVOPS_DB_HOST"]
    user = os.environ.get("DEVOPS_DB_USER", "root")
    pwd = os.environ["DEVOPS_DB_PASSWORD"]

    owner = connect(host, user, pwd, OWNER_DB)
    changed = 0
    for table in SYNC_TABLES:
        base_type = table_type(owner, OWNER_DB, table)
        if base_type != "BASE TABLE":
            print(f"SKIP {table}: owner 侧不是基表（{base_type}）")
            continue
        cols = columns(owner, OWNER_DB, table)
        if not cols:
            print(f"SKIP {table}: owner 侧无列信息")
            continue
        select_list = ", ".join(f"`{OWNER_DB}`.`{table}`.`{c}` AS `{c}`" for c in cols)
        ddl = (f"CREATE OR REPLACE VIEW `{table}` AS SELECT {select_list} "
               f"FROM `{OWNER_DB}`.`{table}`")
        for target in TARGET_DBS:
            conn = connect(host, user, pwd, target)
            try:
                cur_type = table_type(conn, target, table)
                # 目标侧若是基表/不存在才建视图；已是视图则重建（列快照刷新）
                if cur_type == "BASE TABLE":
                    print(f"SKIP {target}.{table}: 目标是基表，不覆盖")
                    continue
                if args.dry_run:
                    print(f"DRY  {target}.{table}: would CREATE OR REPLACE VIEW ({len(cols)} 列)")
                    continue
                with conn.cursor() as c:
                    c.execute(ddl)
                print(f"OK   {target}.{table}: 视图已重建（{len(cols)} 列，原状态 {cur_type or '不存在'}）")
                changed += 1
            finally:
                conn.close()
    owner.close()
    print(f"完成：{changed} 个视图{'（dry-run 未执行）' if args.dry_run else '已更新'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())