#!/usr/bin/env python3
"""P3 写通道覆盖度盘点（W1/W2/W3 × public 写函数）。

输入：docs/weekly-11/cross-domain-write-channel.csv（30+1 张跨域写表）
扫描：src/domains/<owner>/public/__init__.py 中的写函数（INSERT/UPDATE/DELETE/REPLACE
      字面量命中 owner 表），以及 `_xwrite_apply` 的 action 白名单。

输出：docs/weekly-11/write-channel-coverage.md
判据（方案 §9 P3）：
  · W2 每张表至少 1 个幂等 public 写函数；W3 每张表至少 1 个 outbox action；
  · W1 表在非归属域代码中的写引用 = 0（由 foreign-refs 棘轮另行守）。

用法：
  python3 scripts/check_write_channel_coverage.py            # 刷新报告
  python3 scripts/check_write_channel_coverage.py --check    # W2/W3 缺函数即失败
"""
from __future__ import annotations

import argparse
import ast
import csv
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHANNEL_CSV = REPO / "docs" / "weekly-11" / "cross-domain-write-channel.csv"
OUT_MD = REPO / "docs" / "weekly-11" / "write-channel-coverage.md"
PUBLIC = {d: REPO / "src" / "domains" / d / "public" / "__init__.py"
          for d in ("mgmt", "runtime", "insight")}
_WRITE_RE = re.compile(
    r"\b(INSERT\s+(?:IGNORE\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"`?(?:[A-Za-z_]\w*`?\s*\.\s*`?)?([A-Za-z_]\w*)`?", re.I)


def scan_public(domain: str) -> dict[str, list[str]]:
    """返回 {表: [函数名...]}：函数体内出现对该表的写语句即计入。"""
    path = PUBLIC[domain]
    out: dict[str, list[str]] = {}
    if not path.is_file():
        return out
    tree = ast.parse(path.read_text(encoding="utf-8"))
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        segment = "\n".join(lines[node.lineno - 1: node.end_lineno or node.lineno])
        for _verb, table in _WRITE_RE.findall(segment):
            out.setdefault(table, []).append(node.name)
    return {t: sorted(set(fns)) for t, fns in out.items()}


def scan_actions() -> dict[str, list[str]]:
    """扫描 mgmt/runtime 路由的 xwrite action 白名单 → {动作: [域...]}。"""
    actions: dict[str, list[str]] = {}
    for domain in ("mgmt", "runtime"):
        path = REPO / "src" / "domains" / domain / "routes" / "internal.py"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'^\s*"([a-z0-9_]+)":\s*lambda', text, re.M):
            actions.setdefault(m.group(1), []).append(domain)
    return actions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="W1/W2/W3 写通道覆盖度")
    ap.add_argument("--check", action="store_true", help="W2/W3 缺 public 写函数即失败")
    args = ap.parse_args(argv)

    with CHANNEL_CSV.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)]
    public = {d: scan_public(d) for d in ("mgmt", "runtime", "insight")}
    actions = scan_actions()

    lines = ["# 跨域写通道覆盖度（P3 自动生成）", "",
             f"> 输入：`{CHANNEL_CSV.relative_to(REPO)}`；public 写函数扫描自 "
             "`src/domains/<owner>/public/__init__.py`；action 白名单扫描自 "
             "`src/domains/<d>/routes/internal.py`。", "",
             "| 表 | 归属 | 通道 | 写引用 | 写方 | public 写函数 | 覆盖 |",
             "| --- | --- | --- | ---: | --- | --- | --- |"]
    missing_w2: list[str] = []
    missing_w3: list[str] = []
    covered = 0
    for r in rows:
        table, owner, channel = r["table"], r["owner"], r["channel"]
        fns = public.get(owner, {}).get(table, [])
        ok = bool(fns)
        if channel == "W2" and not ok:
            missing_w2.append(table)
        if channel == "W3" and not ok:
            missing_w3.append(table)
        if ok:
            covered += 1
        lines.append(
            f"| `{table}` | {owner} | {channel} | {r['write_refs']} | {r['write_by']} | "
            f"{', '.join('`'+f+'`' for f in fns) or '—'} | {'✅' if ok else '❌'} |")
    lines += ["", f"合计：{len(rows)} 张表，{covered} 张已有 owner public 写函数 "
              f"（{covered * 100 // max(1, len(rows))}%）。", ""]
    if missing_w2:
        lines.append(f"W2 缺口（{len(missing_w2)}）：" + "、".join(f"`{t}`" for t in missing_w2))
    if missing_w3:
        lines.append(f"W3 缺口（{len(missing_w3)}）："
                     + "、".join(f"`{t}`" for t in missing_w3)
                     + "（W3 可由 outbox action 承接，未实现前应保持同库写）")
    lines += ["", "已登记 xwrite action："
              + ("、".join(f"`{a}`（{'+'.join(d)}）" for a, d in sorted(actions.items())) or "无"),
              ""]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_MD.relative_to(REPO)}：{covered}/{len(rows)} 覆盖，"
          f"W2 缺 {len(missing_w2)}，W3 缺 {len(missing_w3)}，action {len(actions)} 个")

    if args.check and (missing_w2 or missing_w3):
        print(f"FAIL: W2 缺 {missing_w2[:10]}；W3 缺 {missing_w3[:10]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())