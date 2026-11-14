#!/usr/bin/env python3
"""同步领域库的跨库视图（幂等）。

背景：库按域拆分后，非 owner 角色通过跨库视图访问他域基表
（`docs/weekly-09/domain-db-split.md`）。视图是**建视图时**的列快照，
所以基表新增列（如 `agent_tasks.trace_context`）或新增表
（`grape_t_credential_usage_log` / `grape_t_system_credentials` /
`grape_t_repo_credentials`）都必须同步重建视图，否则非 owner 侧读该列会 1054、
读新表会 1146。

用法（CLI > 环境变量 > 默认值）：

  DEVOPS_DB_HOST=<db-host> DEVOPS_DB_USER=<user> DEVOPS_DB_PASSWORD=<password> \\
    python3 scripts/sync_domain_views.py [--dry-run] \\
      [--owner <owner-db>] [--target <db1,db2>] [--tables <t1,t2>]

  # dev 示例（库名与生产不同）
  ... python3 scripts/sync_domain_views.py \\
        --owner grape_dev_runtime --target grape_dev_mgmt \\
        --tables grape_t_credential_usage_log,grape_t_system_credentials

可选环境变量：
  GRAPE_OWNER_DB      owner 域库名（默认 grape_runtime_db）
  GRAPE_TARGET_DBS    目标库名，逗号分隔（默认 grape_mgmt_db,grape_insight_db）
  GRAPE_SYNC_TABLES   需要同步的表，逗号分隔（默认见 DEFAULT_SYNC_TABLES）

目标映射（owner 可配置，默认 grape_runtime_db）：
  <target dbs>  ← views for owner tables

**只读边界**：本脚本建的是单表简单视图，MySQL 下技术上可写；但按"谁拥有谁写"口径，
视图**只用于读**，写入必须走 owner 的内部接口（阶段 2）。非 owner 库账号对该视图
只应授予 SELECT —— 这是部署纪律，不是脚本强制的约束。
"""
import argparse
import os
import sys

import pymysql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from view_ddl_builder import ViewDDLError, build_view_ddl, check_ident  # noqa: E402

DEFAULT_OWNER_DB = "grape_runtime_db"
DEFAULT_TARGET_DBS = ("grape_mgmt_db", "grape_insight_db")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ALLOWLIST = os.path.join(_REPO_ROOT, "docs", "weekly-11", "view-allowlist.txt")
DEFAULT_SYNC_TABLES = (
    "agent_tasks",
    "grape_t_credential_usage_log",
    # 2026-11-14 P0：grape_t_system_credentials 的跨域视图已删除（代码零跨域引用，
    # 属 60 个快赢视图之一），不再作为默认同步目标；确需恢复须先评审 view-allowlist。
    # 阶段 1 扩展（2026-11-12）：mgmt 侧同样缺这张跨域表 → 启动刷
    # "Table 'grape_dev_mgmt.grape_t_repo_credentials' doesn't exist"，VCS 凭据链路受影响
    "grape_t_repo_credentials",
    # 接管会话（weekly-11）：owner=runtime，mgmt 的接入路由与 insight 只读
    "takeover_sessions",
)

# 库名/表名直接拼进 DDL（反引号包裹），标识符白名单校验复用 view_ddl_builder


def _split_list(raw: str) -> tuple[str, ...]:
    """逗号分隔 → 去空白、去空项、保序去重。"""
    out: list[str] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return tuple(out)



def resolve_config(owner=None, target=None, tables=None, env=None):
    """解析 owner/targets/tables（CLI > env > 默认），并做标识符校验。

    Returns: (owner_db, target_dbs tuple, tables tuple)
    Raises: ValueError（空值或非法标识符）
    """
    env = os.environ if env is None else env

    # None（未提供）→ env → 默认；显式空串 → 报错（避免"想指定却没生效"静默回落）
    if owner is not None:
        owner_db = owner.strip()
    elif env.get("GRAPE_OWNER_DB"):
        owner_db = env["GRAPE_OWNER_DB"].strip()
    else:
        owner_db = DEFAULT_OWNER_DB

    if target is not None:
        target_dbs = _split_list(target)
        if not target_dbs:
            raise ValueError("target 库名不得为空")
    else:
        target_dbs = _split_list(env.get("GRAPE_TARGET_DBS", "")) or DEFAULT_TARGET_DBS

    if tables is not None:
        sync_tables = _split_list(tables)
        if not sync_tables:
            raise ValueError("tables 不得为空")
    else:
        sync_tables = _split_list(env.get("GRAPE_SYNC_TABLES", "")) or DEFAULT_SYNC_TABLES

    owner_db = check_ident("owner 库名", owner_db)
    for db in target_dbs:
        check_ident("target 库名", db)
    for tbl in sync_tables:
        check_ident("表名", tbl)
    return owner_db, target_dbs, sync_tables


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


def load_allowlist(path: str) -> set[str]:
    """读 `<库>.<视图>` 白名单（P0 冻结：只减不增）。

    文件缺失 → 返回空集合 ⇒ 任何建视图请求都被拒（fail-closed）。
    """
    allowed: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                allowed.add(line)
    except OSError:
        return set()
    return allowed


def allowed_for(allowed: set[str], target_db: str) -> set[str]:
    prefix = target_db + "."
    return {v.split(".", 1)[1] for v in allowed if v.startswith(prefix)}


def prune_unlisted(host: str, user: str, pwd: str, target_dbs, allowed, dry_run: bool) -> int:
    """删除不在白名单里的跨域视图（P5 清零用；P0 用于删 60 个快赢视图）。"""
    dropped = 0
    for target in target_dbs:
        conn = connect(host, user, pwd, target)
        try:
            with conn.cursor() as c:
                c.execute(
                    "SELECT TABLE_NAME FROM information_schema.views WHERE TABLE_SCHEMA=%s"
                    " ORDER BY TABLE_NAME", (target,))
                names = [r[0] for r in c.fetchall()]
            keep = allowed_for(allowed, target)
            for name in names:
                if name in keep:
                    continue
                if dry_run:
                    print(f"DRY  DROP VIEW {target}.{name}（不在白名单）")
                    dropped += 1
                    continue
                with conn.cursor() as c:
                    c.execute(f"DROP VIEW IF EXISTS `{target}`.`{name}`")
                print(f"DROP {target}.{name}（不在白名单）")
                dropped += 1
        finally:
            conn.close()
    return dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    # CLI > 环境变量 > 默认值；生产库名（grape_runtime_db 等）与 dev 库名（grape_dev_*）不同，
    # 硬编码会让 dev/其他环境连错库（2026-11-12 实测）。
    ap.add_argument("--owner", default=None,
                    help="owner 域库名（默认 grape_runtime_db / GRAPE_OWNER_DB）")
    ap.add_argument("--target", default=None,
                    help="目标库名，逗号分隔（默认 grape_mgmt_db,grape_insight_db / GRAPE_TARGET_DBS）")
    ap.add_argument("--tables", default=None,
                    help="需要同步视图的表，逗号分隔（默认见 DEFAULT_SYNC_TABLES / GRAPE_SYNC_TABLES）")
    ap.add_argument("--allowlist", default=DEFAULT_ALLOWLIST,
                    help="视图白名单（默认 docs/weekly-11/view-allowlist.txt；只减不增）")
    ap.add_argument("--prune", action="store_true",
                    help="删除目标库中不在白名单里的视图（P0 快赢 / P5 清零；配合 --dry-run 先看）")
    ap.add_argument("--skip-allowlist", action="store_true",
                    help="应急：跳过白名单校验（仅本地调试，CI/生产禁用）")
    args = ap.parse_args()

    try:
        owner_db, target_dbs, sync_tables = resolve_config(args.owner, args.target, args.tables)
    except (ValueError, ViewDDLError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2
    print(f"owner={owner_db} targets={target_dbs} tables={sync_tables}")
    allowed = load_allowlist(args.allowlist)
    if not args.skip_allowlist:
        print(f"allowlist={args.allowlist} entries={len(allowed)}")

    host = os.environ["DEVOPS_DB_HOST"]
    user = os.environ.get("DEVOPS_DB_USER", "root")
    pwd = os.environ["DEVOPS_DB_PASSWORD"]

    owner = connect(host, user, pwd, owner_db)
    changed = 0
    denied = 0
    for table in sync_tables:
        base_type = table_type(owner, owner_db, table)
        if base_type != "BASE TABLE":
            print(f"SKIP {table}: owner 侧不是基表（{base_type}）")
            continue
        cols = columns(owner, owner_db, table)
        if not cols:
            print(f"SKIP {table}: owner 侧无列信息")
            continue
        ddl = build_view_ddl(owner_db, table, cols)
        for target in target_dbs:
            if not args.skip_allowlist and table not in allowed_for(allowed, target):
                print(f"DENY {target}.{table}: 不在视图白名单（只减不增；"
                      f"如需保留请评审后改 {args.allowlist}）")
                denied += 1
                continue
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
    if args.prune:
        prune_dbs = tuple(dict.fromkeys(tuple(target_dbs) + (owner_db,)))
        dropped = prune_unlisted(host, user, pwd, prune_dbs, allowed, args.dry_run)
        print(f"prune：{dropped} 个视图{'待删除（dry-run）' if args.dry_run else '已删除'}")
    if denied:
        print(f"[FAIL] {denied} 个建视图请求被白名单拒绝", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
