"""盘点 / 守卫「外域表引用」：三库独立化（P0 冻结）用。

两种用法：

1) 盘点（给排优先级、刷新矩阵）
   mysql ... -N -e "SELECT table_schema, table_name FROM information_schema.tables
     WHERE table_schema IN ('grape_mgmt_db','grape_runtime_db','grape_insight_db')
       AND table_type='BASE TABLE';" > /tmp/owner.tsv
   python3 scripts/foreign_table_refs.py --owner /tmp/owner.tsv --src src --out /tmp/matrix.csv

   离线（CI / 无数据库）时用随仓维护的归属清单：
   python3 scripts/foreign_table_refs.py --owner-json configs/table_ownership.json --out /tmp/matrix.csv

2) 棘轮守卫（P0-2）：基线只减不增
   python3 scripts/foreign_table_refs.py --owner-json configs/table_ownership.json \
       --baseline docs/weekly-11/foreign-refs-allowlist.csv --check
   # 外域引用全部清零后，--check 变绿即为 G4 达成；--update-baseline 用于**下降**后刷新基线

判定口径（只用于排序与棘轮，不作最终验收口径的替代）：
  代码里出现 `FROM/JOIN/INTO/UPDATE/DELETE FROM/TABLE <table>` 记一次引用；
  消费域按文件路径判定：src/domains/<d>/ → <d>；src/api/ → api；src/kernel/ → kernel；
  其余（apps/、scripts/ 等）→ other，不计入棘轮（不属于四进程运行代码）。
  只有「非归属域」的引用才算跨域引用。
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

# 一次扫描抽全部引用：SQL 关键字 + 可选 schema 前缀 + 表名
_REF_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM|TABLE)\s+"
    r"(?:`?[A-Za-z_][\w$]*`?\s*\.\s*)?"
    r"`?([A-Za-z_][\w$]*)`?",
    re.I,
)
# DDL 是「建表/改表/删表/改名」，不是跨域数据访问：由 table_ownership 的 DDL 守卫单独治理。
# 不剔除会把 ALTER TABLE tasks RENAME TO issue 之类的迁移语句计成业务引用
# （2026-11-15：legacy rename 让 tasks←api 多出 4 处假引用）。
_DDL_RE = re.compile(
    r"\b(?:CREATE|ALTER|DROP|RENAME)\s+(?:TEMPORARY\s+)?TABLE\s+"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?`?[A-Za-z_][\w$]*`?",
    re.I,
)
_SCHEMA_TO_DOMAIN = {
    "grape_mgmt_db": "mgmt",
    "grape_runtime_db": "runtime",
    "grape_insight_db": "insight",
}
BASELINE_FIELDS = ["table", "owner_domain", "consumer", "refs"]


def load_owner(path: Path) -> dict[str, str]:
    """owner.tsv：每行 `<schema>\t<table>`。"""
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


def load_owner_json(path: Path) -> dict[str, str]:
    """configs/table_ownership.json → {table: domain}（离线口径）。

    _per_domain_tables 里的名字（每域各建一份，如 grape_t_schema_migrations /
    grape_t_xwrite_log）不属于跨域引用，直接排除，否则每域自用自己的表会被算成违规。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    per_domain = set(((data.get("_per_domain_tables") or {}).get("tables") or []))
    owner: dict[str, str] = {}
    for table, domain in (data.get("tables") or {}).items():
        if domain in ("mgmt", "runtime", "insight") and table not in per_domain:
            owner[table] = domain
    return owner


def domain_of(rel: Path) -> str:
    parts = rel.parts
    if parts and parts[0] == "domains" and len(parts) >= 2:
        return parts[1]
    if parts and parts[0] in ("api", "kernel"):
        return parts[0]
    return "other"


def scan(src: Path, owner: dict[str, str]) -> list[dict]:
    """返回按引用数降序的跨域引用行（只含存在非归属域消费的表）。"""
    refs: collections.Counter = collections.Counter()
    files: dict[str, set[str]] = collections.defaultdict(set)
    known = set(owner)
    for path in sorted(src.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(src)
        dom = domain_of(rel)
        found = collections.Counter()
        for name in _REF_RE.findall(_DDL_RE.sub(" ", text)):
            if name in known:
                found[name] += 1
        for table, n in found.items():
            refs[(table, dom)] += n
            files[table].add(str(Path(src.name) / rel))
    rows = []
    for table, own in owner.items():
        if not files.get(table):
            continue
        by: collections.Counter = collections.Counter()
        for (t, d), n in refs.items():
            if t == table:
                by[d] += n
        if set(by) <= {own}:
            continue
        rows.append({
            "table": table,
            "owner_domain": own,
            "sql_references": sum(by.values()),
            "consumer_domains": ";".join(f"{d}:{n}" for d, n in by.most_common()),
            "consumer_files": len(files[table]),
            "_by": dict(by),
            "_files": sorted(files[table]),
        })
    rows.sort(key=lambda r: (-r["sql_references"], r["table"]))
    return rows


def cross_pairs(rows: list[dict]) -> dict[tuple[str, str], int]:
    """(table, consumer) → refs，只保留非归属域、且属于四进程代码的消费方。"""
    pairs: dict[tuple[str, str], int] = {}
    for row in rows:
        own = row["owner_domain"]
        for consumer, n in row["_by"].items():
            if consumer == own or consumer == "other":
                continue
            pairs[(row["table"], consumer)] = n
    return pairs


def load_baseline(path: Path) -> dict[tuple[str, str], int]:
    base: dict[tuple[str, str], int] = {}
    if not path.is_file():
        return base
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("table") or not row.get("consumer"):
                continue
            base[(row["table"].strip(), row["consumer"].strip())] = int(row["refs"])
    return base


def write_baseline(path: Path, rows: list[dict]) -> dict[tuple[str, str], int]:
    pairs = cross_pairs(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BASELINE_FIELDS)
        w.writeheader()
        for (table, consumer), n in sorted(pairs.items()):
            w.writerow({
                "table": table,
                "owner_domain": next(r["owner_domain"] for r in rows if r["table"] == table),
                "consumer": consumer,
                "refs": n,
            })
    return pairs


def check(rows: list[dict], baseline: dict[tuple[str, str], int]) -> tuple[list[str], list[str]]:
    """返回 (violations, improvements)。新 pair 或计数上升 → violation。"""
    current = cross_pairs(rows)
    violations: list[str] = []
    improvements: list[str] = []
    for key, n in sorted(current.items()):
        allowed = baseline.get(key)
        if allowed is None:
            violations.append(f"新增跨域引用 {key[0]}←{key[1]}: {n} 处（未在 allowlist）")
        elif n > allowed:
            violations.append(f"跨域引用上升 {key[0]}←{key[1]}: {allowed} → {n}")
        elif n < allowed:
            improvements.append(f"下降 {key[0]}←{key[1]}: {allowed} → {n}")
    for key in sorted(set(baseline) - set(current)):
        improvements.append(f"清零 {key[0]}←{key[1]}: {baseline[key]} → 0")
    return violations, improvements


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="盘点/守卫外域表引用（三库独立化）")
    src_group = ap.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--owner", help="owner.tsv：schema\\ttable 每行一条")
    src_group.add_argument("--owner-json", help="configs/table_ownership.json（离线归属清单）")
    ap.add_argument("--src", default="src", help="源码根目录（默认 src）")
    ap.add_argument("--out", default="", help="输出盘点 CSV 路径（缺省打印前 30 行）")
    ap.add_argument("--baseline", default="docs/weekly-11/foreign-refs-allowlist.csv",
                    help="棘轮基线 CSV（默认 docs/weekly-11/foreign-refs-allowlist.csv）")
    ap.add_argument("--check", action="store_true", help="对比基线：新增/上升即失败（exit 1）")
    ap.add_argument("--update-baseline", action="store_true",
                    help="用当前结果重写基线（只应在引用数**下降**后执行）")
    args = ap.parse_args(argv)

    if args.owner_json:
        owner = load_owner_json(Path(args.owner_json))
    else:
        owner = load_owner(Path(args.owner))
    rows = scan(Path(args.src), owner)
    baseline_path = Path(args.baseline)
    base = load_baseline(baseline_path)

    if args.out:
        fields = ["table", "owner_domain", "sql_references", "consumer_domains", "consumer_files"]
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {len(rows)} rows to {args.out}")
    else:
        for r in rows[:30]:
            print({k: v for k, v in r.items() if not k.startswith("_")})

    total = sum(r["sql_references"] for r in rows)
    cross_total = sum(cross_pairs(rows).values())
    print(f"tables={len(rows)} sql_references={total} cross_domain_refs={cross_total} "
          f"pairs={len(cross_pairs(rows))} baseline={baseline_path}"
          # 只有表头的基线是清零后的合法终态，不能标 (missing) —— 那会让人以为文件丢了
          f"{' (missing)' if not baseline_path.exists() else ''}")

    if args.update_baseline:
        if base:
            violations, _ = check(rows, base)
            if violations:
                print("拒绝刷新基线：存在新增/上升的外域引用，先修代码：", file=sys.stderr)
                for v in violations:
                    print("  " + v, file=sys.stderr)
                return 1
        pairs = write_baseline(baseline_path, rows)
        print(f"baseline updated: {len(pairs)} pairs, {sum(pairs.values())} refs")
        return 0

    if args.check:
        # 只有表头的基线是**合法**状态（跨域引用清零后的目标形态），不能当成文件缺失：
        # 否则清零之后棘轮会静默失效，未来新增跨域引用也没人拦。
        if not baseline_path.exists():
            print(f"基线文件不存在：{baseline_path}", file=sys.stderr)
            return 2
        violations, improvements = check(rows, base)
        for i in improvements[:20]:
            print("  ↓ " + i)
        if len(improvements) > 20:
            print(f"  ↓ ... 共 {len(improvements)} 项下降/清零")
        if violations:
            print(f"FAIL: {len(violations)} 项跨域引用新增/上升", file=sys.stderr)
            for v in violations:
                print("  ✗ " + v, file=sys.stderr)
            return 1
        print("OK: 外域引用未上升（棘轮通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())