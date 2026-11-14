#!/usr/bin/env python3
"""三库独立化 · 剩余工作清单（可重复跑的"燃烧"循环）。

把三份证据合成一张按优先级排序的待办：
  1. 静态面：每张表还剩多少跨域 SQL（表×消费域），以及集中在哪些文件；
  2. 运行面：stage 冒烟（读 + 写场景）失败项；
  3. 归属面：跨域表清单与 owner 分布（P1 逐表收口的输入）。

`--loop N`：连续跑 N 轮（默认 1），每轮重新扫描并打印剩余量；用于改一批→看一次余量。
退出码：0 = 三项全清零；1 = 仍有剩余（清单已打印）。

用法：
  python3 scripts/rds_burndown.py                    # 只跑静态面（无需 stage）
  python3 scripts/rds_burndown.py --smoke            # 叠加 stage 冒烟（需 stage DSN）
  python3 scripts/rds_burndown.py --loop 3 --smoke   # 连跑 3 轮看余量变化
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OWNER = REPO / "docs/weekly-11/table-owner.tsv"
BASELINE = REPO / "docs/weekly-11/foreign-refs-allowlist.csv"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("ftr", REPO / "scripts/foreign_table_refs.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def static_backlog() -> tuple[list[dict], int]:
    """按表统计剩余跨域引用（消费域不在 owner 域内即计入）。"""
    scanner = _load_scanner()
    owner = scanner.load_owner(OWNER)
    rows = scanner.scan(REPO / "src", owner)
    backlog = []
    total = 0
    for r in rows:
        by = {d: n for d, n in r["_by"].items() if d not in (r["owner_domain"], "other")}
        n = sum(by.values())
        if not n:
            continue
        total += n
        files = [f for f in r["_files"] if not f.startswith(f"src/domains/{r['owner_domain']}/")]
        backlog.append({"table": r["table"], "owner": r["owner_domain"], "refs": n,
                        "consumers": by, "files": files[:5]})
    backlog.sort(key=lambda x: -x["refs"])
    return backlog, total


def baseline_pairs() -> int:
    if not BASELINE.is_file():
        return -1
    with BASELINE.open(newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.DictReader(f))


def smoke_backlog() -> tuple[list[dict], int]:
    """跑 stage 冒烟（读+写），返回失败项。"""
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts/stage_api_smoke.py"), "--with-ids", "--write", "--json"],
        capture_output=True, text=True, cwd=REPO, timeout=900)
    # smoke 的 JSON 是缩进多行输出（日志走 stderr），按整段解析；兜底从头找 '{'
    payload = None
    out = (proc.stdout or "").strip()
    for candidate in (out, out[out.find("{"):] if "{" in out else ""):
        try:
            cand = json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(cand, dict) and "total" in cand:
            payload = cand
            break
    if payload is None:
        return [{"path": "<smoke 未产出 JSON>", "error": (proc.stderr or "")[-300:]}], 1
    failed = payload.get("failed") or []
    return failed, len(failed)


def report(it: int, smoke: bool) -> int:
    backlog, total = static_backlog()
    print(f"\n===== 第 {it} 轮 =====")
    print(f"[静态] 剩余跨域 SQL：{total} 处 / {len(backlog)} 张表（基线 pair 数 {baseline_pairs()}）")
    for row in backlog[:12]:
        cons = ",".join(f"{d}:{n}" for d, n in sorted(row["consumers"].items(), key=lambda kv: -kv[1]))
        print(f"  {row['refs']:>4}  {row['table']:<32} owner={row['owner']:<8} ← {cons}")
        for f in row["files"][:3]:
            print(f"        {f}")
    if len(backlog) > 12:
        print(f"  … 其余 {len(backlog) - 12} 张表见 docs/weekly-11/independent-rds-foreign-table-matrix.csv")

    smoke_fail = 0
    if smoke:
        failed, smoke_fail = smoke_backlog()
        print(f"[运行] stage 冒烟失败：{smoke_fail}")
        for f in failed[:15]:
            print(f"  {f.get('status')} {f.get('path')}  {str(f.get('error'))[:100]}")

    if total == 0 and smoke_fail == 0:
        print("\nRESULT: 清零（静态 0 / 运行 0）")
        return 0
    print(f"\nRESULT: 未清零（静态 {total} / 运行 {smoke_fail}）")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="三库独立化剩余工作清单")
    ap.add_argument("--loop", type=int, default=1, help="连续跑几轮（默认 1）")
    ap.add_argument("--smoke", action="store_true", help="叠加 stage 冒烟（需要 stage DSN）")
    args = ap.parse_args(argv)
    rc = 1
    for it in range(1, max(1, args.loop) + 1):
        rc = report(it, args.smoke)
        if rc == 0:
            break
    return rc


if __name__ == "__main__":
    raise SystemExit(main())