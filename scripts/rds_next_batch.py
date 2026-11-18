#!/usr/bin/env python3
"""为「下一批」生成可直接执行的作业单（文件 → 表 → 行号 → 现有原语是否够用）。

为什么要它：跨域收口剩下的都是「动态 SQL / 迁移逻辑」，不能机械替换；每批开工前都要先回答
三个问题——这个文件碰了哪些外域表、每张表在哪些行、runtime/mgmt 现有具名查询能不能覆盖。
这个脚本把这三件事一次算完，输出 markdown（`--out` 落盘）或纯文本。

用法：
  python3 scripts/rds_next_batch.py            # top 5 文件
  python3 scripts/rds_next_batch.py --top 10 --out docs/weekly-11/next-batches.md
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OWNER_TSV = REPO / "configs/table_ownership.json"
PUBLIC_GLOB = "src/domains/{domain}/public/__init__.py"
DOMAIN_KW = {"mgmt": "mgmt", "runtime": "runtime", "insight": "insight"}

#: 已确立口径、短期不再尝试的文件（原因写清处，避免下一轮又按处数硬拿）。
#: 本清单**随生成器持久化**——作业单每次重写都不会把它冲掉（历史教训：
#: 2026-11-14 起连续四轮把同一批 blocked 理由写进台账，作业单一刷新就丢标记，
#: 每轮都要重新复核一遍同一结论）。
BLOCKED: dict[str, str] = {
    "src/kernel/prompt_builder.py": (
        "3 处是 agent 提示文案（f-string 里的说明文本，本进程不执行，静态正则才计到）；"
        "`tests/runtime/test_issue_372_unified_mapping_github.py` 按源文本断言该段语义；"
        "改写前必须先给 agent 一个合法的映射写通道。口径：**不为掉数字改文案**（台账 2026-11-14）"
    ),
    "src/kernel/persistence/credential_usage_log.py": (
        "3 处 flush/retention 由后台线程共用 owner 连接 → `Packet sequence number wrong`；"
        "正解是「每工作线程一条 owner 连接」的池化，属独立一轮（交接文档 §5.3 坑 4 / §9 阻塞项 2）"
    ),
}


def load_scanner():
    spec = importlib.util.spec_from_file_location("ftr", REPO / "scripts/foreign_table_refs.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def named_queries(domain: str) -> set[str]:
    """目标域 public 里已登记的具名查询（_ANY_QUERIES / _READ_QUERIES 的键）。"""
    path = REPO / PUBLIC_GLOB.format(domain=domain)
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    keys: set[str] = set()
    for m in re.finditer(r'_ANY_QUERIES[^=]*=\s*\{(.*?)\n\}', text, re.S):
        keys.update(re.findall(r'"([a-z0-9_]+)"\s*:', m.group(1)))
    for m in re.finditer(r'_READ_QUERIES[^=]*=\s*\{(.*?)\n\}', text, re.S):
        keys.update(re.findall(r'"([a-z0-9_]+)"\s*:', m.group(1)))
    return keys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成下一批作业单")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    scanner = load_scanner()
    owner = scanner.load_owner(OWNER_TSV)
    queries = {d: named_queries(d) for d in DOMAIN_KW}

    rows: list[tuple[int, str, dict[str, list[int]], str]] = []
    for path in sorted((REPO / "src").rglob("*.py")):
        rel = path.relative_to(REPO / "src")
        dom = scanner.domain_of(rel)
        by_table: dict[str, list[int]] = collections.defaultdict(list)
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for name in scanner._REF_RE.findall(line):
                if name in owner and owner[name] != dom:
                    by_table[name].append(lineno)
        if by_table:
            total = sum(len(v) for v in by_table.values())
            rows.append((total, str(path.relative_to(REPO)), by_table, dom))

    rows.sort(reverse=True)
    out: list[str] = ["# 下一批：跨域收口作业单", ""]
    out.append(f"总量 {sum(r[0] for r in rows)} 处 / {len(rows)} 个文件；下表取前 {args.top} 个。")
    out.append("")
    live_blocked = {p: why for p, why in BLOCKED.items()
                    if any(r[1] == p for r in rows)}
    if live_blocked:
        out.append("## ⛔ blocked（口径已确立，不要按处数硬拿）")
        for path, why in live_blocked.items():
            out.append(f"- `{path}`：{why}")
        out.append("")
    for total, path, by_table, dom in rows[: args.top]:
        mark = "⛔ " if path in BLOCKED else ""
        out.append(f"## {mark}{path}（{dom} 域，{total} 处）")
        if path in BLOCKED:
            out.append("- **blocked**：见上方 ⛔ 小节；本轮取第一个未 blocked 的文件。")
        for table, lines in sorted(by_table.items(), key=lambda kv: -len(kv[1])):
            target = owner[table]
            have = queries.get(target, set())
            hint = f"{target} public 已登记 {len(have)} 条具名查询" if have else f"{target} public 无具名查询，需新增"
            shown = ", ".join(str(n) for n in lines[:12]) + (" …" if len(lines) > 12 else "")
            out.append(f"- `{table}`（归 {target}）× {len(lines)}：行 {shown}")
            out.append(f"  - {hint}")
        out.append("")
    out.append("## 每批验收口径（不要跳）")
    out.append("1) `python3 scripts/foreign_table_refs.py --owner configs/table_ownership.json "
               "--baseline docs/weekly-11/foreign-refs-allowlist.csv --check`")
    out.append("2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`")
    out.append("3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）")
    out.append("4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）")
    out.append("5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧")
    text = "\n".join(out) + "\n"
    if args.out:
        (REPO / args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}（{sum(r[0] for r in rows)} 处 / {len(rows)} 文件）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())