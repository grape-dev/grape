#!/usr/bin/env python3
"""生成/校验内部端点契约快照（方案 §6.3）。

对外契约（`/api/*`）走 `src/api/openapi.py` 的 `/openapi.json`，**内部端点不进对外契约**
（`include_in_schema=False`，避免经 /docs 暴露）。因此内部端点需要独立快照：

  docs/weekly-11/internal-contract.json
    {"routes": [{domain, method, path, operation_id, summary}...]}

命名约束：`operation_id` 必须形如 `<domain>_<resource>_<action>`（CLI 按 operationId 生成）。

用法：
  python3 scripts/gen_internal_contract.py            # 刷新快照
  python3 scripts/gen_internal_contract.py --check    # 漂移即失败（CI）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "weekly-11" / "internal-contract.json"
sys.path.insert(0, str(REPO))

# 挂载口径必须与 src/api/server_common.attach_internal_routes 一致
_DOMAIN_ROUTERS = (
    ("mgmt", "src.domains.mgmt.routes.internal", ("readonly_router", "xwrite_router")),
    ("runtime", "src.domains.runtime.routes.internal", ("router",)),
    ("insight", "src.domains.insight.routes.internal", ("router",)),
)
_OP_ID_RE = re.compile(r"^(mgmt|runtime|insight)_[a-z0-9_]+$")


def collect() -> dict:
    routes = []
    for domain, module, attrs in _DOMAIN_ROUTERS:
        mod = __import__(module, fromlist=["*"])
        for attr in attrs:
            router = getattr(mod, attr, None)
            if router is None:
                continue
            for r in router.routes:
                path = getattr(r, "path", "")
                if not path.startswith("/internal"):
                    continue
                for method in sorted(getattr(r, "methods", set()) or set()):
                    if method in ("HEAD", "OPTIONS"):
                        continue
                    routes.append({
                        "domain": domain,
                        "method": method,
                        "path": re.sub(r"\{(\w+):\w+\}", r"{\1}", path),
                        "operation_id": getattr(r, "operation_id", None) or "",
                        "summary": (getattr(r, "summary", None) or "").strip(),
                    })
    routes.sort(key=lambda r: (r["domain"], r["path"], r["method"]))
    # 去重（同一 router 被两个 attr 导出时）
    seen, unique = set(), []
    for r in routes:
        key = (r["domain"], r["method"], r["path"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return {"routes": unique, "counts": _counts(unique)}


def _counts(routes: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in routes:
        out[r["domain"]] = out.get(r["domain"], 0) + 1
    return out


def render(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def validate(data: dict) -> list[str]:
    errs = []
    for r in data["routes"]:
        if not r["operation_id"]:
            errs.append(f"{r['domain']} {r['method']} {r['path']}: 缺 operation_id")
        elif not _OP_ID_RE.match(r["operation_id"]):
            errs.append(f"{r['domain']} {r['method']} {r['path']}: operation_id "
                        f"'{r['operation_id']}' 不满足 domain_resource_action")
        elif not r["operation_id"].startswith(r["domain"] + "_"):
            errs.append(f"{r['domain']} {r['method']} {r['path']}: operation_id "
                        f"前缀与域不符（{r['operation_id']}）")
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="内部端点契约快照")
    ap.add_argument("--check", action="store_true", help="与已提交快照不一致即失败")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)

    data = collect()
    errs = validate(data)
    if errs:
        for e in errs:
            print("✗ " + e, file=sys.stderr)
        return 1
    text = render(data)
    out = Path(args.out)
    if args.check:
        if not out.is_file():
            print(f"快照缺失：{out}", file=sys.stderr)
            return 1
        if out.read_text(encoding="utf-8") != text:
            print("内部契约漂移：请运行 python3 scripts/gen_internal_contract.py", file=sys.stderr)
            return 1
        print(f"内部契约最新（{len(data['routes'])} 条：{data['counts']}）")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out.relative_to(REPO)}（{len(data['routes'])} 条：{data['counts']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())