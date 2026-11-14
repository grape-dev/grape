#!/usr/bin/env python3
"""生成「跨域依赖矩阵看板」（P0-4）。

输入（随仓维护，可离线跑）：
  docs/weekly-11/table-owner.tsv                     P0 冻结归属清单
  docs/weekly-11/cross-domain-disposition.csv        读/写处置分类（D/C1/C2/B/E）
  docs/weekly-11/cross-domain-write-channel.csv      写通道分配（W1/W2/W3/X）
  scripts/foreign_table_refs.py                      实际代码扫描（唯一计数口径）

输出：
  docs/weekly-11/dependency-matrix.md                可评审、可 diff 的矩阵
  docs/weekly-11/dependency-board.html                看板（域×域热力 + 明细 + 进度）

用法：
  python3 scripts/gen_domain_dependency_board.py            # 生成并覆盖
  python3 scripts/gen_domain_dependency_board.py --check    # 只校验（CI 用：内容过期即失败）
"""
from __future__ import annotations

import argparse
import csv
import datetime
import html
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs" / "weekly-11"
OWNER_TSV = DOCS / "table-owner.tsv"
DISPOSITION = DOCS / "cross-domain-disposition.csv"
CHANNEL = DOCS / "cross-domain-write-channel.csv"
BASELINE = DOCS / "foreign-refs-allowlist.csv"
MD_OUT = DOCS / "dependency-matrix.md"
HTML_OUT = DOCS / "dependency-board.html"
DOMAINS = ("mgmt", "runtime", "insight", "api", "kernel")


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "foreign_table_refs", REPO / "scripts" / "foreign_table_refs.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _csv_map(path: Path, key: str) -> dict[str, dict]:
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {r[key]: r for r in csv.DictReader(f) if r.get(key)}


def collect() -> dict:
    scanner = _load_scanner()
    owner = scanner.load_owner(OWNER_TSV)
    rows = scanner.scan(REPO / "src", owner)
    dispositions = _csv_map(DISPOSITION, "table")
    channels = _csv_map(CHANNEL, "table")
    baseline = scanner.load_baseline(BASELINE)

    matrix: dict[tuple[str, str], int] = defaultdict(int)
    for (table, consumer), n in scanner.cross_pairs(rows).items():
        matrix[(owner[table], consumer)] += n

    items = []
    for r in rows:
        table = r["table"]
        own = r["owner_domain"]
        by = {d: n for d, n in r["_by"].items() if d != own and d != "other"}
        items.append({
            "table": table,
            "owner": own,
            "refs": r["sql_references"],
            "cross_refs": sum(by.values()),
            "files": r["consumer_files"],
            "by": by,
            "disposition": dispositions.get(table, {}).get("disposition", ""),
            "channel": channels.get(table, {}).get("channel", ""),
        })
    items.sort(key=lambda x: (-x["cross_refs"], x["table"]))
    return {
        "items": items,
        "matrix": dict(matrix),
        "total_refs": sum(r["sql_references"] for r in rows),
        "cross_refs": sum(baseline.values()),
        "pairs": len(baseline),
        "tables": len(items),
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _phase_stats(items: list[dict]) -> list[tuple[str, int, int]]:
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for it in items:
        key = (it["disposition"] or "未分类").split(" ", 1)[0]
        buckets[key][0] += 1
        buckets[key][1] += it["cross_refs"]
    order = ["D", "C1", "C2", "B", "E", "未分类"]
    return [(k, buckets[k][0], buckets[k][1]) for k in order if k in buckets]


def render_md(data: dict) -> str:
    m = data["matrix"]
    lines = [
        "# 跨域依赖矩阵看板（P0-4 自动生成）",
        "",
        f"> 生成时间：{data['generated_at']}　口径：`scripts/foreign_table_refs.py`（只算 api/kernel/domains 运行代码）",
        f"> 汇总：跨域表 **{data['tables']}** 张；跨域引用 **{data['cross_refs']}** 处（{data['pairs']} 个「表×消费域」对）；"
        f"含归属域在内的 SQL 引用 {data['total_refs']} 处",
        "",
        "## 1. 域 × 域 跨域引用热力",
        "",
        "| 消费方 ↓ / 归属域 → | mgmt | runtime | insight | 合计 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for consumer in ("api", "kernel", "mgmt", "runtime", "insight"):
        cells = [m.get((owner, consumer), 0) for owner in ("mgmt", "runtime", "insight")]
        if not any(cells):
            continue
        lines.append(f"| {consumer} | {cells[0]} | {cells[1]} | {cells[2]} | {sum(cells)} |")
    lines += ["", "## 2. 处置分类进度", "", "| 分类 | 表数 | 剩余跨域引用 |", "| --- | ---: | ---: |"]
    for key, tables, refs in _phase_stats(data["items"]):
        lines.append(f"| {key} | {tables} | {refs} |")
    lines += [
        "",
        "## 3. 全量明细（按剩余跨域引用降序）",
        "",
        "| 表 | 归属 | 跨域引用 | 消费方 | 处置 | 写通道 | 文件数 |",
        "| --- | --- | ---: | --- | --- | --- | ---: |",
    ]
    for it in data["items"]:
        consumers = " / ".join(f"{d} {n}" for d, n in sorted(it["by"].items(), key=lambda kv: -kv[1]))
        lines.append(
            f"| `{it['table']}` | {it['owner']} | {it['cross_refs']} | {consumers} | "
            f"{it['disposition']} | {it['channel'] or '—'} | {it['files']} |")
    lines += [
        "",
        "## 4. 用法",
        "",
        "```bash",
        "python3 scripts/gen_domain_dependency_board.py          # 刷新看板",
        "python3 scripts/gen_domain_dependency_board.py --check  # 过期即失败（CI）",
        "```",
        "",
        "验收口径：G4「代码零外域表引用」= 本表 `跨域引用` 列全为 0 且 "
        "`docs/weekly-11/foreign-refs-allowlist.csv` 为空。",
    ]
    return "\n".join(lines) + "\n"


def render_html(data: dict) -> str:
    m = data["matrix"]
    max_cell = max(m.values() or [1])

    def cell(owner: str, consumer: str) -> str:
        n = m.get((owner, consumer), 0)
        if not n:
            return "<td class='z'>·</td>"
        alpha = 0.12 + 0.78 * (n / max_cell)
        return f"<td class='h' style='background:rgba(255,99,71,{alpha:.2f})'>{n}</td>"

    rows_html = []
    for consumer in ("api", "kernel", "mgmt", "runtime", "insight"):
        cells = [cell(o, consumer) for o in ("mgmt", "runtime", "insight")]
        total = sum(m.get((o, consumer), 0) for o in ("mgmt", "runtime", "insight"))
        if not total:
            continue
        rows_html.append(f"<tr><th>{consumer}</th>{''.join(cells)}<td class='tot'>{total}</td></tr>")

    detail = []
    for it in data["items"]:
        consumers = ", ".join(f"{d} {n}" for d, n in sorted(it["by"].items(), key=lambda kv: -kv[1]))
        detail.append(
            "<tr>"
            f"<td class='mono'>{html.escape(it['table'])}</td>"
            f"<td>{it['owner']}</td>"
            f"<td class='num'>{it['cross_refs']}</td>"
            f"<td class='small'>{html.escape(consumers)}</td>"
            f"<td class='small'>{html.escape(it['disposition'])}</td>"
            f"<td class='small mono'>{html.escape(it['channel'] or '—')}</td>"
            "</tr>")

    phase = "".join(
        f"<div class='card'><div class='k'>{html.escape(k)}</div>"
        f"<div class='v'>{tables}<span> 张表</span></div>"
        f"<div class='s'>剩余 {refs} 处</div></div>"
        for k, tables, refs in _phase_stats(data["items"]))

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>跨域依赖矩阵看板 · Grape 三库独立化</title>
<style>
:root{{--bg:#0b0f14;--panel:#121820;--line:#1f2a36;--fg:#e6edf3;--mut:#8b9bb0;--accent:#ff7a59}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Noto Sans SC",sans-serif;padding:28px}}
h1{{font-size:22px;margin:0 0 4px}}
.meta{{color:var(--mut);font-size:13px;margin-bottom:22px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}}
.card .k{{color:var(--mut);font-size:12px;letter-spacing:.04em}}
.card .v{{font-size:26px;font-weight:600;margin-top:2px}}
.card .v span{{font-size:12px;color:var(--mut);font-weight:400}}
.card .s{{font-size:12px;color:var(--mut)}}
section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:20px}}
h2{{font-size:15px;margin:0 0 12px;color:var(--fg)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}}
th{{color:var(--mut);font-weight:500;font-size:12px}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
td.h{{text-align:center;font-variant-numeric:tabular-nums;font-weight:600}}
td.z{{text-align:center;color:#4a5666}}
td.tot{{text-align:right;font-weight:600}}
.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}}
.small{{font-size:12px;color:var(--mut)}}
.scroll{{max-height:520px;overflow:auto}}
</style></head><body>
<h1>跨域依赖矩阵看板</h1>
<div class="meta">Grape 三库独立 RDS 升级 · 生成于 {data['generated_at']} · 口径 scripts/foreign_table_refs.py（只算运行代码）</div>
<div class="cards">
  <div class="card"><div class="k">跨域表</div><div class="v">{data['tables']}<span> 张</span></div><div class="s">需归属重定/接口化</div></div>
  <div class="card"><div class="k">跨域引用</div><div class="v">{data['cross_refs']}<span> 处</span></div><div class="s">{data['pairs']} 个「表×消费域」对</div></div>
  <div class="card"><div class="k">SQL 总引用</div><div class="v">{data['total_refs']}<span> 处</span></div><div class="s">含归属域自身</div></div>
  <div class="card"><div class="k">目标</div><div class="v">0</div><div class="s">G4：外域引用归零</div></div>
</div>
<section><h2>处置分类进度</h2><div class="cards">{phase}</div></section>
<section><h2>域 × 域 跨域引用热力（行=消费方，列=归属域）</h2>
<table><thead><tr><th>消费方 ↓ / 归属域 →</th><th>mgmt</th><th>runtime</th><th>insight</th><th>合计</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table></section>
<section><h2>全量明细（{data['tables']} 张，按剩余引用降序）</h2><div class="scroll">
<table><thead><tr><th>表</th><th>归属</th><th>跨域引用</th><th>消费方</th><th>处置</th><th>写通道</th></tr></thead>
<tbody>{''.join(detail)}</tbody></table></div></section>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成跨域依赖矩阵看板")
    ap.add_argument("--check", action="store_true", help="只校验产物是否最新（过期 exit 1）")
    args = ap.parse_args(argv)

    data = collect()
    md = render_md(data)
    page = render_html(data)

    if args.check:
        stale = []
        if not MD_OUT.is_file() or MD_OUT.read_text(encoding="utf-8") != md:
            stale.append(str(MD_OUT.relative_to(REPO)))
        if not HTML_OUT.is_file() or HTML_OUT.read_text(encoding="utf-8") != page:
            stale.append(str(HTML_OUT.relative_to(REPO)))
        if stale:
            print("看板过期，请运行 python3 scripts/gen_domain_dependency_board.py："
                  + ", ".join(stale), file=sys.stderr)
            return 1
        print("看板最新")
        return 0

    MD_OUT.write_text(md, encoding="utf-8")
    HTML_OUT.write_text(page, encoding="utf-8")
    print(f"wrote {MD_OUT.relative_to(REPO)} / {HTML_OUT.relative_to(REPO)}；"
          f"{data['tables']} 表 / {data['cross_refs']} 跨域引用 / {data['pairs']} 对")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())