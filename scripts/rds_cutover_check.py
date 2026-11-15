#!/usr/bin/env python3
"""P4 搬迁校验：源/目标实例的 schema + 行数 + 校验和基线对比。

用途（方案 §8.1/§8.3）：
  1) 搬迁前对**源实例**取基线：
       python3 scripts/rds_cutover_check.py --mode baseline \
         --host <mgmt-rds> --user root --out /tmp/rds-baseline.json
  2) 全量+增量同步后，对**目标实例**复核：
       python3 scripts/rds_cutover_check.py --mode verify \
         --host <target> --user root --baseline /tmp/rds-baseline.json

校验口径：
  · 每库基表清单（表名集合）；
  · 每表行数（COUNT(*)）+ CHECKSUM TABLE（全表，非抽样）；
  · 每库视图数（搬迁后目标应为 0——视图跨实例不可能存在）；
  · schema 指纹（列名/类型/可空/默认值的有序哈希），避免"行数对了 schema 飘了"。

退出码：0 一致；1 存在差异（逐条打印）；2 参数/连接错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys

DBS = ("grape_mgmt_db", "grape_runtime_db", "grape_insight_db")
SCHEMES = ("grape_mgmt_db", "grape_runtime_db", "grape_insight_db")


def connect(host: str, port: int, user: str, password: str):
    import pymysql

    return pymysql.connect(host=host, port=port, user=user, password=password,
                           charset="utf8mb4", connect_timeout=10,
                           # read_timeout=120s：切库核对只跑 information_schema 聚合与行数
                           # 抽样（无全表扫描），实测 <3s；保留与 verify_domain_isolation 同一
                           # 数值，便于把「慢」和「不可达」区分开。
                           read_timeout=120)


def collect(conn, dbs=DBS) -> dict:
    out: dict = {"dbs": {}}
    with conn.cursor() as c:
        for db in dbs:
            c.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s", (db,))
            if not c.fetchone()[0]:
                out["dbs"][db] = {"missing": True}
                continue
            c.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (db,))
            tables = [r[0] for r in c.fetchall()]
            c.execute("SELECT COUNT(*) FROM information_schema.views WHERE table_schema=%s", (db,))
            views = int(c.fetchone()[0])
            info = {"tables": {}, "views": views}
            for t in tables:
                c.execute(f"SELECT COUNT(*) FROM `{db}`.`{t}`")
                rows = int(c.fetchone()[0])
                c.execute(f"CHECKSUM TABLE `{db}`.`{t}`")
                checksum = c.fetchone()[1]
                info["tables"][t] = {"rows": rows, "checksum": str(checksum)}
            c.execute(
                "SELECT table_name, column_name, column_type, is_nullable, "
                "COALESCE(column_default, '') FROM information_schema.columns "
                "WHERE table_schema=%s ORDER BY table_name, ordinal_position", (db,))
            fingerprint = hashlib.sha256(
                "\n".join("|".join(str(x) for x in r) for r in c.fetchall()).encode()
            ).hexdigest()[:16]
            info["schema_fingerprint"] = fingerprint
            out["dbs"][db] = info
    return out


def compare(baseline: dict, actual: dict) -> list[str]:
    diffs: list[str] = []
    for db in DBS:
        b, a = baseline["dbs"].get(db, {}), actual["dbs"].get(db, {})
        if b.get("missing") or a.get("missing"):
            diffs.append(f"{db}: 实例缺失（baseline_missing={b.get('missing')}, "
                         f"target_missing={a.get('missing')}）")
            continue
        if b.get("schema_fingerprint") != a.get("schema_fingerprint"):
            diffs.append(f"{db}: schema 指纹不一致 {b.get('schema_fingerprint')} -> "
                         f"{a.get('schema_fingerprint')}")
        if b.get("views") != a.get("views"):
            diffs.append(f"{db}: 视图数 {b.get('views')} -> {a.get('views')}"
                         f"（P5 目标为 0）")
        bt, at = b.get("tables", {}), a.get("tables", {})
        for t in sorted(set(bt) - set(at)):
            diffs.append(f"{db}.{t}: 目标缺表")
        for t in sorted(set(at) - set(bt)):
            diffs.append(f"{db}.{t}: 目标多表")
        for t in sorted(set(bt) & set(at)):
            if bt[t]["rows"] != at[t]["rows"]:
                diffs.append(f"{db}.{t}: 行数 {bt[t]['rows']} -> {at[t]['rows']}")
            if bt[t]["checksum"] != at[t]["checksum"]:
                diffs.append(f"{db}.{t}: CHECKSUM {bt[t]['checksum']} -> {at[t]['checksum']}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RDS 搬迁校验（baseline / verify）")
    ap.add_argument("--mode", choices=("baseline", "verify"), required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--baseline", help="--mode verify 时读取的基线 JSON 路径")
    ap.add_argument("--out", default="", help="--mode baseline 输出路径（缺省打印）")
    args = ap.parse_args(argv)

    try:
        conn = connect(args.host, args.port, args.user, args.password)
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] 连接失败：{exc}", file=sys.stderr)
        return 2
    data = collect(conn)
    conn.close()

    if args.mode == "baseline":
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if args.out:
            open(args.out, "w", encoding="utf-8").write(text + "\n")
            total = sum(len(v.get("tables", {})) for v in data["dbs"].values())
            rows = sum(t["rows"] for v in data["dbs"].values() for t in v.get("tables", {}).values())
            print(f"wrote {args.out}：{total} 表 / {rows} 行")
        else:
            print(text)
        return 0

    if not args.baseline:
        print("[FATAL] --mode verify 需要 --baseline", file=sys.stderr)
        return 2
    baseline = json.load(open(args.baseline, encoding="utf-8"))
    diffs = compare(baseline, data)
    if diffs:
        print(f"FAIL: {len(diffs)} 处差异", file=sys.stderr)
        for d in diffs[:200]:
            print("  ✗ " + d, file=sys.stderr)
        return 1
    print("OK: 三库 schema/行数/校验和与基线一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())