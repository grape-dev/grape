#!/usr/bin/env python3
"""F811（redefined-while-unused）棘轮门禁（Issue #4158）。

背景：`scripts/check_duplicate_defs.py` 只看**顶层同名 def/class**，看不见 import 别名 /
局部重定义这类 F811 形态。Issue #4158 的事故是顶层同名副本覆盖，但同一类「后定义顶掉前定义」
在 ruff 里还有 72 处存量（`runtime/routes/chat.py` 9、`mgmt/routes/grape_auth.py` 8、
`mgmt/user/db.py` 5 等），此前**没有任何门禁调用** `ruff check --select F811`——
`pyproject.toml` 里的 dev 依赖只是「装了才用」，静默失效（PR #2077 复审 Optional）。

规则：当前 F811 命中数 > `configs/f811_baseline.json` 的 total 即 FAIL。
棘轮只允许减少：清零或下降后跑 `--update-baseline` 收紧，基线随之下降，杜绝长期存量变成
「反正有基线」的垃圾桶（与 `check_async_blocking.py` / `check_undefined_names.py` 同款口径）。

ruff 不可用时**显式失败**，不伪装成通过——门禁静默降级正是 Issue #4158 要修的病症。

用法：
    python3 scripts/check_f811.py                  # 校验（CI / 本地）
    python3 scripts/check_f811.py --list           # 打印全部命中明细
    python3 scripts/check_f811.py --update-baseline  # 收紧基线（仅在命中数下降时）
    python3 scripts/check_f811.py tests            # 指定扫描根（默认 src）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "configs" / "f811_baseline.json"


def _rel(filename: str) -> str:
    try:
        return str(Path(filename).resolve().relative_to(REPO))
    except Exception:  # noqa: BLE001 - 绝对路径归一失败时保留原值，仍可定位
        return filename


def _key(hit: dict) -> str:
    return f"{hit['file']}:{hit['line']}"


def collect(target: str) -> list[dict]:
    """调用 ruff 收集 F811 命中；执行失败/输出不可解析一律抛错（显式失败）。"""
    cmd = [sys.executable, "-m", "ruff", "check", "--select", "F811",
           "--output-format", "json", "--no-cache", target]
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=300)
    except FileNotFoundError as exc:
        raise RuntimeError("ruff 不可用（dev extra 未安装）→ 门禁显式失败，不跳过") from exc
    # ruff: 0 = 无命中，1 = 有命中；其它退出码是执行错误
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"ruff 执行失败 rc={proc.returncode}: {proc.stderr.strip()[:400]}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ruff 输出不是 JSON（{exc}）: {proc.stdout.strip()[:200]}") from exc

    hits: list[dict] = []
    for row in rows:
        rel = _rel(row.get("filename") or row.get("file") or "")
        nested = row.get("diagnostics") or []
        if nested:  # 新版 ruff 可能按文件分组输出
            for diag in nested:
                loc = diag.get("location") or {}
                hits.append({"file": rel, "line": int(loc.get("row") or 0),
                             "code": diag.get("code") or "F811",
                             "message": diag.get("message") or ""})
        elif row.get("location"):
            loc = row.get("location") or {}
            hits.append({"file": rel, "line": int(loc.get("row") or 0),
                         "code": row.get("code") or "F811",
                         "message": row.get("message") or ""})
    return sorted(hits, key=_key)


def load_baseline() -> dict:
    if not BASELINE.exists():
        return {"total": 0, "items": []}
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 基线损坏按 0 处理（会让门禁更严，不静默放宽）
        return {"total": 0, "items": []}


def write_baseline(hits: list[dict]) -> None:
    """基线存条目快照（file:line），这样新增项能被精确指出，而不是只报「多了几处」。"""
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(
        json.dumps({"total": len(hits), "items": sorted(_key(h) for h in hits)},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="src", help="扫描根（默认 src）")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--list", action="store_true", dest="show_all")
    args = ap.parse_args()

    try:
        hits = collect(args.target)
    except RuntimeError as exc:
        print(f"✗ F811 gate: {exc}", file=sys.stderr)
        return 1

    if args.update_baseline:
        write_baseline(hits)
        print(f"baseline updated: {len(hits)} 处（{BASELINE.relative_to(REPO)}）")

    if args.show_all or args.update_baseline:
        for hit in hits:
            print(f"  {_key(hit)}  [{hit['code']}] {hit['message']}")

    base = load_baseline()
    base_total = int(base.get("total") or 0)
    base_items = set(base.get("items") or [])
    print(f"F811 gate: 当前 {len(hits)} / 基线 {base_total}（{args.target}）")

    if len(hits) > base_total:
        print(f"✗ 新增 {len(hits) - base_total} 处 F811 重定义（基线 {base_total}）：",
              file=sys.stderr)
        new = [h for h in hits if _key(h) not in base_items] if base_items else hits
        for hit in new[:20]:
            print(f"  ✗ {_key(hit)}  {hit['message']}", file=sys.stderr)
        print("修复：后定义不得静默顶掉前定义——合并为一份实现或改用别名 import。",
              file=sys.stderr)
        return 1

    if len(hits) < base_total:
        print(f"  ↓ 比基线少 {base_total - len(hits)} 处，可 --update-baseline 收紧")
    print("OK: F811 未新增")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())