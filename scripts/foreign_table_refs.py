"""盘点「外域表引用」：给三库独立化排优先级用。

输入：owner.tsv（每行 `<schema>\t<table>`，来自 information_schema.tables 的基表清单）。
输出：CSV（table,owner_domain,sql_references,consumer_domains,consumer_files）。

判定口径（粗粒度，只用于排序，不作验收）：
  代码里出现 `FROM/JOIN/INTO/UPDATE/DELETE FROM/TABLE <table>` 才算一次引用，
  避免把 "meta"、"skill" 这类普通单词误算。消费域按文件路径判定：
  src/domains/<d>/ → <d>；src/api/ → api；src/kernel/ → kernel。

用法::

    mysql ... -N -e "SELECT table_schema, table_name FROM information_schema.tables
      WHERE table_schema IN ('grape_mgmt_db','grape_runtime_db','grape_insight_db')
        AND table_type='BASE TABLE';" > /tmp/owner.tsv

    python3 scripts/foreign_table_refs.py --owner /tmp/owner.tsv --src src --out /tmp/matrix.csv
"""
from __future__ import annotations

import argparse
import collections
import csv
import re
from pathlib import Path

SQL_KW = r"(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM|TABLE)"
_SCHEMA_TO_DOMAIN = {
    "grape_mgmt_db": "mgmt",
    "grape_runtime_db": "runtime",
    "grape_insight_db": "insight",
}


def load_owner(path: Path) -> dict[str, str]:
    owner: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            continue
        schema, table = parts[0].strip(), parts[1].strip()
        domain = _SCHEMA_TO_DOMAIN.get(schema)
        if domain:
            owner[table] = domain
    return owner


def domain_of(rel: Path) -> str:
    """相对 src/ 的路径 → 域：domains/<d>/... → <d>；api/... → api；kernel/... → kernel。"""
    parts = rel.parts
    if parts and parts[0] == "domains" and len(parts) >= 2:
        return parts[1]
    if parts and parts[0] in ("api", "kernel"):
        return parts[0]
    return "other"


def scan(src: Path, owner: dict[str, str]) -> list[dict]:
    refs: collections.Counter = collections.Counter()
    files: dict[str, set[str]] = collections.defaultdict(set)
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(src)
        dom = domain_of(rel)
        for table in owner:
            hits = len(re.findall(rf"{SQL_KW}\s+[`\"]?{re.escape(table)}[`\"]?\b", text, re.I))
            if hits:
                refs[(table, dom)] += hits
                files[table].add(str(Path(src.name) / rel))
    rows = []
    for table, own in owner.items():
        by: collections.Counter = collections.Counter()
        for (t, d), n in refs.items():
            if t == table:
                by[d] += n
        if not files.get(table):
            continue
        if set(by) <= {own}:
            continue  # 只有归属域自己读 → 不是跨域依赖
        rows.append({
            "table": table,
            "owner_domain": own,
            "sql_references": sum(by.values()),
            "consumer_domains": ";".join(f"{d}:{n}" for d, n in by.most_common()),
            "consumer_files": len(files[table]),
        })
    rows.sort(key=lambda r: -r["sql_references"])
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="盘点外域表引用（三库独立化用）")
    ap.add_argument("--owner", required=True, help="owner.tsv：schema\ttable 每行一条")
    ap.add_argument("--src", default="src", help="源码根目录（默认 src）")
    ap.add_argument("--out", default="", help="输出 CSV 路径（缺省打印前 30 行）")
    args = ap.parse_args(argv)
    owner = load_owner(Path(args.owner))
    rows = scan(Path(args.src), owner)
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["table", "owner_domain", "sql_references",
                                              "consumer_domains", "consumer_files"])
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {len(rows)} rows to {args.out}")
    else:
        for r in rows[:30]:
            print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
