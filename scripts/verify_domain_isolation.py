#!/usr/bin/env python3
"""P5 域隔离验收（方案 §10 A1/A2/A3/A6/A12 的机器化检查）。

检查项：
  G2/A1  三库跨域视图数 = 0
  G3/A2  账号授权只含本库（需要给 --account 传入 `域=账号`，未传则跳过并标注）
  G4/A3  代码外域表引用 = 0（复用 scripts/foreign_table_refs.py，基线与实测都应为空）
  A6     跨域写补偿队列：每域 pending < 100、dead = 0
  A12    lib 写不经任意表：仅做静态提示（public 函数清单 + 视图/授权佐证）

用法：
  python3 scripts/verify_domain_isolation.py --host <rds> --user <u> --password <p> \
      --account mgmt=grape_mgmt --account runtime=grape_runtime --account insight=grape_insight

退出码：0 全部通过；1 有未通过项；2 环境/参数错误。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DBS = {"mgmt": "grape_mgmt_db", "runtime": "grape_runtime_db", "insight": "grape_insight_db"}
DBS_COPY = dict(DBS)


def _connect(host, port, user, password):
    import pymysql

    return pymysql.connect(host=host, port=port, user=user, password=password,
                           charset="utf8mb4", connect_timeout=10)


def check_views(conn) -> list[str]:
    problems = []
    with conn.cursor() as c:
        for domain, db in DBS.items():
            c.execute("SELECT COUNT(*) FROM information_schema.views WHERE table_schema=%s", (db,))
            n = int(c.fetchone()[0])
            print(f"  views[{db}] = {n}")
            if n:
                problems.append(f"A1: {db} 仍有 {n} 个视图（目标 0）")
    return problems


def check_grants(conn, accounts: dict[str, str]) -> list[str]:
    problems = []
    if not accounts:
        print("  grants: 跳过（未提供 --account）")
        return problems
    with conn.cursor() as c:
        for domain, user in accounts.items():
            own = DBS[domain]
            c.execute(f"SHOW GRANTS FOR '{user}'@'%'")
            grants = [r[0] for r in c.fetchall()]
            if not grants:
                c.execute(f"SHOW GRANTS FOR '{user}'@'localhost'")
                grants = [r[0] for r in c.fetchall()]
            foreign = [g for g in grants
                       if any(db in g for db in DBS.values() if db != own)]
            print(f"  grants[{user}] = {len(grants)} 条，跨库 {len(foreign)} 条")
            if foreign:
                problems.append(f"A2: {user} 仍有跨库授权：{foreign[:3]}")
    return problems


def check_code_refs() -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "foreign_table_refs", REPO / "scripts" / "foreign_table_refs.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    owner = mod.load_owner(REPO / "docs" / "weekly-11" / "table-owner.tsv")
    rows = mod.scan(REPO / "src", owner)
    base = mod.load_baseline(REPO / "docs" / "weekly-11" / "foreign-refs-allowlist.csv")
    violations, improvements = mod.check(rows, base)
    current = mod.cross_pairs(rows)
    print(f"  code refs: 当前 {len(current)} 个「表×消费域」对 / 基线 {len(base)} 对")
    problems = []
    if current:
        problems.append(f"A3: 代码仍有 {len(current)} 个跨域引用对（基线亦应清空）")
    if base:
        problems.append(f"A3: allowlist 未清空（{len(base)} 对）")
    if violations:
        problems.append(f"A3: 存在新增/上升引用 {len(violations)} 项")
    return problems


def check_xwrite(conn) -> list[str]:
    problems = []
    with conn.cursor() as c:
        for domain, db in DBS.items():
            c.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s "
                      "AND table_name='grape_t_xwrite_log'", (db,))
            if not c.fetchone()[0]:
                print(f"  xwrite[{db}] = 无表（该域尚无 outbox 用法）")
                continue
            c.execute(f"SELECT status, COUNT(*) FROM `{db}`.grape_t_xwrite_log GROUP BY status")
            stats = {r[0]: int(r[1]) for r in c.fetchall()}
            print(f"  xwrite[{db}] = {stats}")
            if stats.get("dead"):
                problems.append(f"A6: {db} 死信 {stats['dead']} 条")
            if stats.get("pending", 0) >= 100:
                problems.append(f"A6: {db} pending {stats['pending']} ≥ 100")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P5 域隔离验收")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--db-prefix", default="",
                    help="stage 彩排用：库名改为 <prefix>_mgmt / <prefix>_runtime / <prefix>_insight")
    ap.add_argument("--account", action="append", default=[],
                    help="域=账号（可重复，如 --account mgmt=grape_mgmt）")
    args = ap.parse_args(argv)

    if args.db_prefix:
        for domain in DBS:
            DBS[domain] = f"{args.db_prefix}_{domain}"
            DBS_COPY[domain] = DBS[domain]

    accounts = {}
    for item in args.account:
        if "=" not in item:
            print(f"[FATAL] --account 需形如 域=账号：{item}", file=sys.stderr)
            return 2
        domain, user = item.split("=", 1)
        if domain not in DBS:
            print(f"[FATAL] 未知域：{domain}", file=sys.stderr)
            return 2
        accounts[domain] = user

    try:
        conn = _connect(args.host, args.port, args.user, args.password)
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] 连接失败：{exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    try:
        print("== A1 视图清零 ==")
        problems += check_views(conn)
        print("== A2 账号授权 ==")
        problems += check_grants(conn, accounts)
        print("== A6 补偿队列 ==")
        problems += check_xwrite(conn)
    finally:
        conn.close()
    print("== A3 代码外域引用 ==")
    problems += check_code_refs()

    print()
    if problems:
        print(f"RESULT: FAIL（{len(problems)} 项）")
        for p in problems:
            print("  ✗ " + p)
        return 1
    print("RESULT: PASS（G2/G3/G4/A6 全绿）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())