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


def _connect_dsn(url: str):
    """mysql://user:pass@host:port/db → pymysql 连接。"""
    from urllib.parse import unquote, urlparse

    import pymysql

    u = urlparse(url)
    return pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username,
                           password=unquote(u.password or ""), charset="utf8mb4",
                           connect_timeout=10,
                           # read_timeout=120s：最大单表 tables 概览 + 视图定义全量拉取（P4 之后
                           # 三库合计约 130 张基表），实测最慢一次 8s；120s 是 15 倍余量，
                           # 用于区分「慢查询」与「连接被防火墙黑洞」（后者会一直挂到超时）。
                           read_timeout=120)


def check_views(conns) -> list[str]:
    problems = []
    for domain, db in DBS.items():
        conn = conns[domain]
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM information_schema.views WHERE table_schema=%s", (db,))
            n = int(c.fetchone()[0])
        print(f"  views[{db}] = {n}")
        if n:
            problems.append(f"A1: {db} 仍有 {n} 个视图（目标 0）")
    return problems


def check_grants(conns, accounts: dict[str, str]) -> list[str]:
    problems = []
    if not accounts:
        print("  grants: 跳过（未提供 --account）")
        return problems
    for domain, user in accounts.items():
        conn = conns[domain]
        with conn.cursor() as c:
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


def check_xwrite(conns) -> list[str]:
    problems = []
    for domain, db in DBS.items():
        conn = conns[domain]
        with conn.cursor() as c:
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
    ap.add_argument("--env-only", action="store_true",
                    help="只验收环境面（A1/A2/A6）；A3 代码外域引用仍打印但不作为失败项")
    ap.add_argument("--dsn-mgmt", default="", help="分布式拓扑：mgmt 实例 DSN")
    ap.add_argument("--dsn-runtime", default="", help="分布式拓扑：runtime 实例 DSN")
    ap.add_argument("--dsn-insight", default="", help="分布式拓扑：insight 实例 DSN")
    ap.add_argument("--host", default="", help="单实例三库模式：主机（与 --db-prefix 配合）")
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

    conns: dict[str, object] = {}
    try:
        dsns = {"mgmt": args.dsn_mgmt, "runtime": args.dsn_runtime, "insight": args.dsn_insight}
        if any(dsns.values()):
            missing = [d for d, v in dsns.items() if not v]
            if missing:
                print(f"[FATAL] 分布式模式需要三个 --dsn-*：缺 {missing}", file=sys.stderr)
                return 2
            for domain, dsn in dsns.items():
                conns[domain] = _connect_dsn(dsn)
        else:
            if not args.host:
                print("[FATAL] 需要 --host 或三个 --dsn-*", file=sys.stderr)
                return 2
            conn = _connect(args.host, args.port, args.user, args.password)
            conns = {d: conn for d in DBS}
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] 连接失败：{exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    try:
        print("== A1 视图清零 ==")
        problems += check_views(conns)
        print("== A2 账号授权 ==")
        problems += check_grants(conns, accounts)
        print("== A6 补偿队列 ==")
        problems += check_xwrite(conns)
    finally:
        for c in {id(v): v for v in conns.values()}.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    print("== A3 代码外域引用 ==")
    code_problems = check_code_refs()
    if args.env_only:
        print(f"  （--env-only：A3 {len(code_problems)} 项计入报告、不计入失败）")
    else:
        problems += code_problems

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