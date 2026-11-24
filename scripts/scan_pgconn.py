#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘点 / 守卫「进程库连接查本域表」——三库独立（每进程只连本域库）后的错库风险面。

背景
----
分库后每个进程只认自己的库（`DEVOPS_DB_NAME`），跨域访问必须走 owner 域的 lib / DSN。
但域代码里仍可能残留 `from src.kernel.session_manager import db` + `db.cursor().execute(sql)`：
同一段 SQL、同一个文件，在「本域进程」里是对的，被其它进程加载时就会打到**别人的库**
（命中旧副本或直接 1146）。静态跨域扫描（`foreign_table_refs.py`）看不到这类问题——
它只看「文件域 vs 表归属」，而这里文件域与表归属相同、只有连接是进程库。

判定口径
----
- 只统计 `<cursor>.execute / executemany`，且游标来源是 `src.kernel.session_manager.db`；
- SQL 必须引用「本域自有表」（`configs/table_ownership.json` 中 owner == 文件所在域）；
- 已排除安全来源：`src.kernel.domain_db.cursor()`、`db_registry.cursor()`、`*_any_cursor()`、
  `read_one_any / read_all_any`、`owner_db / db_pair() / owner_lock()`；
- 风险面 = 域 ≠ mgmt 的站点（mgmt 域代码只在 mgmt 进程执行，库对得上）。

用法
----
    python3 scripts/scan_pgconn.py                 # 汇总 + top 文件
    python3 scripts/scan_pgconn.py --json          # 机器可读
    python3 scripts/scan_pgconn.py --csv out.csv   # 明细
    python3 scripts/scan_pgconn.py --check         # 对比基线（棘轮：只减不增）
    python3 scripts/scan_pgconn.py --update-baseline  # 下降后刷新基线

退出码：0 = 与基线一致（或 scan 成功）/ 1 = 相对基线有新增 / 2 = fail-closed（清单/源码缺失）。
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MANIFEST = os.path.join(REPO_ROOT, "configs", "table_ownership.json")
BASELINE = os.path.join(REPO_ROOT, "scripts", "pgconn_baseline.json")
SQL_KW = ("select ", "insert ", "update ", "delete ")
RISK_DOMAINS = ("runtime", "insight")


def load_owned() -> dict[str, set[str]]:
    data = json.load(open(MANIFEST, encoding="utf-8"))
    owned: dict[str, set[str]] = {}
    for table, domain in (data.get("tables") or {}).items():
        owned.setdefault(domain, set()).add(table.lower())
    return owned


def const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def origin_of(expr, env: dict[str, str]) -> str:
    """游标来源：pg（进程库）/ domain / owner / unknown。"""
    if isinstance(expr, ast.Name):
        return env.get(expr.id, "unknown")
    if isinstance(expr, ast.Call):
        f = expr.func
        if isinstance(f, ast.Attribute):
            base = f.value
            if isinstance(base, ast.Name) and base.id == "session_manager" and f.attr == "cursor":
                return "pg"
            if isinstance(base, ast.Name) and base.id in ("db", "_db"):
                return env.get(base.id, "unknown")
            if f.attr in ("read_one", "read_all") or f.attr.upper().endswith("_ANY"):
                return "owner"
            name = f.attr
        elif isinstance(f, ast.Name):
            name = f.id
        else:
            name = ""
        if name in ("db_pair", "owner_db", "owner_lock"):
            return "owner"
        if name in ("_any_cursor", "_owner_cursor", "read_one_any", "read_all_any"):
            return "owner"
        # `cursor_for` / `transaction_for` 是内核唯一路由原语（route_of 判定；三库缺 DSN
        # 即 DomainUnavailable，绝不回落进程库）——Issue #3857：此前未登记，靠
        # “未知来源”恰好不被计数，语义上应收口为域来源。
        if name in ("cursor", "_domain_cursor", "cursor_for", "transaction_for"):
            return "domain"
    return "unknown"


def scan_file(path: str, rel: str, domain: str, owned: set[str]) -> list[dict]:
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
    except SyntaxError:
        return []

    # 只用模块层语句构造模块环境（不进函数体，函数内赋值不得泄入）：
    # 模块级的 if/try/with 等控制流体内仍是模块作用域，需递归
    mod_env: dict[str, str] = {}

    def _collect_mod(stmts):
        for n in stmts:
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if n.module and n.module.endswith("session_manager") and a.name in ("db", "_db"):
                        mod_env[a.asname or a.name] = "pg"
            elif isinstance(n, ast.Assign):
                o = origin_of(n.value, mod_env)
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        mod_env[t.id] = o
            elif isinstance(n, (ast.If, ast.Try)):
                for body in (n.body, getattr(n, "orelse", []), getattr(n, "finalbody", [])):
                    _collect_mod(body or [])
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                _collect_mod(n.body)

    _collect_mod(tree.body)

    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    by_name: dict[str, list] = {}
    for fn in funcs:
        by_name.setdefault(fn.name, []).append(fn)

    def _fn_env(fn, seeds: dict[str, str]) -> dict[str, str]:
        env = dict(mod_env)
        env.update(seeds)
        for n in ast.walk(fn):
            if isinstance(n, ast.ImportFrom):
                # 函数内延迟导入（含 db as _pdb 别名）同样构成 pg 来源
                for a in n.names:
                    if n.module and n.module.endswith("session_manager") and a.name in ("db", "_db"):
                        env[a.asname or a.name] = "pg"
            elif isinstance(n, ast.Assign):
                o = origin_of(n.value, env)
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        env[t.id] = o
                    elif isinstance(t, (ast.Tuple, ast.List)):
                        for el in t.elts:
                            if isinstance(el, ast.Name):
                                env[el.id] = o
            elif isinstance(n, ast.AnnAssign) and isinstance(n.value, ast.Call):
                if isinstance(n.target, ast.Name):
                    env[n.target.id] = origin_of(n.value, env)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if item.optional_vars is not None and isinstance(item.optional_vars, ast.Name):
                        env[item.optional_vars.id] = origin_of(item.context_expr, env)
        return env

    # 参数来源传播（不动点）：调用方把 pg 游标按位传给本文件函数时，标记该形参为 pg。
    # 取代旧实现「函数体赋值泄入模块环境」的隐式跨函数传播，消除兄弟作用域混叠。
    pg_params: dict[tuple[str, str], set[str]] = {}
    seeds: dict[str, dict[str, str]] = {}
    for _ in range(5):
        changed = False
        for fn in funcs:
            env = _fn_env(fn, seeds.get(fn.name, {}))
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call) and n.args):
                    continue
                callee = None
                skip_self = 0
                if isinstance(n.func, ast.Name) and n.func.id in by_name:
                    callee = n.func.id
                elif isinstance(n.func, ast.Attribute) and n.func.attr in by_name \
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == "self":
                    callee = n.func.attr
                    skip_self = 1
                if callee is None:
                    continue
                for target in by_name[callee]:
                    params = [a.arg for a in target.args.args]
                    for i, arg in enumerate(n.args):
                        if skip_self + i >= len(params):
                            break
                        o = origin_of(arg, env)
                        key = (callee, params[skip_self + i])
                        if o == "pg" and key not in pg_params:
                            pg_params[key] = set()
                        if o == "pg" and "pg" not in pg_params[key]:
                            pg_params[key].add("pg")
                            seeds.setdefault(callee, {}).setdefault(params[skip_self + i], "pg")
                            changed = True
        if not changed:
            break

    out = []
    for fn in funcs:
        env = _fn_env(fn, seeds.get(fn.name, {}))
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in ("execute", "executemany") and n.args:
                sql = const_str(n.args[0])
                if not sql:
                    continue
                low = " " + sql.lower().replace("\n", " ") + " "
                tables = sorted(t for t in owned
                                if f" {t} " in low or f"`{t}`" in low or f"{t}(" in low)
                if not tables:
                    continue
                if origin_of(n.func.value, env) != "pg":
                    continue
                out.append({
                    "domain": domain,
                    "file": rel,
                    "line": getattr(n, "lineno", 0),
                    "func": fn.name,
                    "tables": ",".join(tables),
                    "sql": " ".join(sql.split())[:120],
                })
    return out


def walk_src() -> list[dict]:
    if not os.path.isdir(os.path.join(REPO_ROOT, "src")):
        print("[FATAL] src/ not found", file=sys.stderr)
        raise SystemExit(2)
    if not os.path.isfile(MANIFEST):
        print(f"[FATAL] ownership manifest missing: {MANIFEST}", file=sys.stderr)
        raise SystemExit(2)
    owned = load_owned()
    sites = []
    for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, "src")):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, REPO_ROOT)
            parts = rel.split(os.sep)
            if len(parts) < 3 or parts[1] != "domains":
                continue
            domain = parts[2]
            sites.extend(scan_file(path, rel, domain, owned.get(domain, set())))
    if not sites:
        print("[FATAL] 0 site scanned — 判据或路径异常（fail-closed）", file=sys.stderr)
        raise SystemExit(2)
    return sites


def summarize(sites: list[dict]) -> dict:
    risk = [s for s in sites if s["domain"] in RISK_DOMAINS]
    by_file: dict[str, int] = {}
    for s in risk:
        by_file[s["file"]] = by_file.get(s["file"], 0) + 1
    return {
        "total_sites": len(sites),
        "risk_sites": len(risk),
        "risk_domains": {d: sum(1 for s in risk if s["domain"] == d) for d in RISK_DOMAINS},
        "files": dict(sorted(by_file.items())),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="pg-connection SQL site scanner")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="对比基线（棘轮：只减不增）")
    g.add_argument("--update-baseline", action="store_true", help="写入基线（下降后刷新）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--csv", metavar="PATH")
    args = ap.parse_args(argv)

    sites = walk_src()
    summary = summarize(sites)

    if args.csv:
        risk = [s for s in sites if s["domain"] in RISK_DOMAINS]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["domain", "file", "line", "func", "tables", "sql"])
            w.writeheader()
            w.writerows(sorted(risk, key=lambda s: (s["file"], s["line"])))
        print(f"CSV -> {args.csv} ({len(risk)} rows)")

    if args.json:
        print(json.dumps({"summary": summary, "sites": sites}, ensure_ascii=False, indent=1))
        return 0

    print(f"进程库 SQL 执行点 {summary['total_sites']}；"
          f"其中风险域（{'/'.join(RISK_DOMAINS)}）{summary['risk_sites']}")
    for f, n in sorted(summary["files"].items(), key=lambda x: -x[1])[:15]:
        print(f"  {n:3d}  {f}")

    if args.update_baseline:
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"baseline updated -> {os.path.relpath(BASELINE, REPO_ROOT)}")
        return 0

    if args.check:
        if not os.path.isfile(BASELINE):
            print(f"[FATAL] baseline missing: {os.path.relpath(BASELINE, REPO_ROOT)}", file=sys.stderr)
            return 2
        base = json.load(open(BASELINE, encoding="utf-8"))
        base_files = base.get("files") or {}
        grew = []
        for f, n in summary["files"].items():
            old = int(base_files.get(f, 0))
            if n > old:
                grew.append(f"{f}: {old} -> {n}")
        if grew:
            print("\n[FAIL] 进程库执行点上升（分库后应改为 owner 域连接）：")
            for g_line in grew:
                print("  " + g_line)
            return 1
        print(f"\nOK: 未上升（基线 {base.get('risk_sites')} / 现 {summary['risk_sites']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())