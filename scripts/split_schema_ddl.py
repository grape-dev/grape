#!/usr/bin/env python3
"""把 db_setup.py 里的建表 DDL 常量按归属域搬到 `src/domains/<域>/schema_ddl.py`。

为什么：`src/api/db_setup.py` 一个文件里放着三域全部 DDL，静态扫描会把它们全算成
「api 消费 runtime/insight 表」（实测 1082/1232 处跨域引用来自这一个文件）。DDL 属于
表的归属域（方案 D9「每域自己的 schema」），搬到 owner 域模块后：
  · 扫描归属 = owner 域，跨域引用归零；
  · 行为零变化——db_setup 仍执行同一批 SQL，只是常量从 owner 模块 import。

做法：AST 找模块级 `NAME = "SQL 字符串"` 常量（含 CREATE TABLE），按表名映射归属域，
原文搬进目标模块，并在 db_setup 里改成显式 import。仅搬纯 SQL 常量，不碰迁移逻辑。

用法：
  python3 scripts/split_schema_ddl.py            # 落地（写文件）
  python3 scripts/split_schema_ddl.py --dry-run  # 只打印计划
校验：搬完用 `init_database()` 在空 sqlite 上重建，比对表名/列名与搬前快照一致。
"""
from __future__ import annotations

import argparse
import ast
import re
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB_SETUP = REPO / "src/api/db_setup.py"
OWNER_TSV = REPO / "configs/table_ownership.json"
TABLE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)[`\"]?", re.I)


SCHEMA_DOMAIN = {"grape_mgmt_db": "mgmt", "grape_runtime_db": "runtime",
                 "grape_insight_db": "insight"}


def load_owner() -> dict[str, str]:
    """table → 归属域（冻结清单 configs/table_ownership.json）。"""
    owner = {}
    for line in OWNER_TSV.read_text(encoding="utf-8").splitlines():
        schema, _, table = line.partition("\t")
        if table and schema in SCHEMA_DOMAIN:
            owner[table.strip()] = SCHEMA_DOMAIN[schema]
    return owner


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DDL 常量按域搬迁")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    owner = load_owner()
    src = DB_SETUP.read_text(encoding="utf-8")
    tree = ast.parse(src)

    picked: dict[str, str] = {}      # const name -> domain
    segments: dict[str, str] = {}    # const name -> source text
    unknown: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        m = TABLE_RE.search(value.value)
        if not m:
            continue
        table = m.group(1)
        domain = owner.get(table)
        if domain not in ("mgmt", "runtime", "insight"):
            unknown.append(f"{target.id} → {table}")
            continue
        picked[target.id] = domain
        segments[target.id] = ast.get_source_segment(src, node) or ""

    by_domain: dict[str, list[str]] = defaultdict(list)
    for name, domain in picked.items():
        by_domain[domain].append(name)

    print(f"[plan] 可搬 DDL 常量 {len(picked)} 个；未知归属 {len(unknown)} 个"
          f"（{', '.join(unknown[:5])}）")
    for domain, names in sorted(by_domain.items()):
        print(f"  {domain}: {len(names)} 个")

    if args.dry_run:
        return 0

    # 1) 写 owner 域模块
    for domain, names in by_domain.items():
        lines = [
            f'"""DDL：{domain} 域基表建表语句（由 db_setup 拆分而来，归属本域）。',
            "",
            "搬迁原因见 scripts/split_schema_ddl.py：DDL 属表的归属域（方案 D9），",
            "放在 owner 域模块里才能让「跨域引用」统计反映真实依赖。",
            "行为不变：db_setup 仍按原顺序执行这些语句。",
            '"""',
            "",
        ]
        for name in names:
            lines.append(segments[name])
            lines.append("")
        out = REPO / "src/domains" / domain / "schema_ddl.py"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"[write] {out.relative_to(REPO)}（{len(names)} 条）")

    # 2) 从 db_setup 删掉这些常量，改成 import
    lines = src.split("\n")
    drop_ranges: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name) and node.targets[0].id in picked:
            start = node.lineno - 1
            end = (node.end_lineno or node.lineno) - 1
            drop_ranges.append((start, end))
    # 附带删除紧邻的空行（保持文件整洁）
    keep = [True] * len(lines)
    for start, end in drop_ranges:
        for i in range(start, end + 1):
            keep[i] = False
        if end + 1 < len(lines) and not lines[end + 1].strip():
            keep[end + 1] = False

    imports = []
    for domain, names in sorted(by_domain.items()):
        joined = ",\n    ".join(names)
        imports.append(f"from src.domains.{domain}.schema_ddl import (\n    {joined},\n)")
    insert_at = next(i for i, line in enumerate(lines) if line.startswith("from src.kernel"))
    out_lines: list[str] = []
    for i, line in enumerate(lines):
        if i == insert_at:
            out_lines.extend(imports + [""])
        if keep[i]:
            out_lines.append(line)
    DB_SETUP.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"[write] {DB_SETUP.relative_to(REPO)}（删除 {len(drop_ranges)} 个常量，改为 import）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())