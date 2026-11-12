#!/usr/bin/env python3
"""四进程路由契约探针：nginx 分流 vs 各进程实际路由。

背景（2026-11-12 事故）：切四进程后 nginx 未同步——
  · /api/events/{seq}/(a2a-redo|chat|knowledge|flow/reset) 被整组代理到事件进程，
    实际只注册在 mgmt → 404（A2A 补发失败）
  · POST /api/pipeline/handoff 无规则，被 `/` 兜底到 mgmt（只有 GET）→ 405

本探针把"nginx 会把这条路径送到哪个进程"与"那个进程是否真的服务它"对齐，
任何不一致即失败（退出码 1），供 CI / 部署后自检使用。

用法：
    python3 scripts/probe/route_contract_probe.py                 # 只查本机进程
    python3 scripts/probe/route_contract_probe.py --public https://grape-dev.topxtopx.com
    python3 scripts/probe/route_contract_probe.py --conf /etc/nginx/conf.d/grape.conf --json

环境变量：
    GRAPE_PROBE_PORTS  "mgmt=8081,event=8082,worker=8083,insight=8084"
    GRAPE_NGINX_CONF   默认 /etc/nginx/conf.d/grape.conf
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_PORTS = {"mgmt": 8081, "event": 8082, "worker": 8083, "insight": 8084}
DEFAULT_CONF = "/etc/nginx/conf.d/grape.conf"
METHODS = ("get", "post", "put", "patch", "delete")


def _ports() -> dict[str, int]:
    raw = os.environ.get("GRAPE_PROBE_PORTS", "").strip()
    if not raw:
        return dict(DEFAULT_PORTS)
    out = {}
    for item in raw.split(","):
        if "=" in item:
            role, port = item.split("=", 1)
            out[role.strip()] = int(port)
    return out or dict(DEFAULT_PORTS)


def _http(url: str, method: str = "GET", timeout: float = 8.0):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("allow", "")
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("allow", "") if e.headers else "")
    except Exception as e:  # 连接失败也算不可达
        return -1, str(e)[:60]


def fetch_routes(port: int) -> dict[str, set[str]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=20) as r:
            spec = json.loads(r.read().decode())
    except Exception:
        return {}
    out: dict[str, set[str]] = {}
    for path, ops in (spec.get("paths") or {}).items():
        if not path.startswith("/api"):
            continue
        out[path] = {m.upper() for m in ops if m in METHODS}
    return out


def parse_nginx(conf: str) -> list[dict]:
    """解析 location 规则（按出现顺序；regex 首个命中、精确优先、前缀最长）。"""
    try:
        text = open(conf, encoding="utf-8").read()
    except OSError:
        return []
    rules = []
    for m in re.finditer(r"location\s+(~\*?|=)?\s*(\S+)\s*\{", text):
        mod = (m.group(1) or "").strip()
        pat = m.group(2)
        start = text.index("{", m.start())
        depth, j = 0, start
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = text[start + 1:j]
        pm = re.search(r"proxy_pass\s+([^;]+);", body)
        if not pm:
            continue
        rules.append({"pos": m.start(),
                      "kind": "regex" if mod in ("~", "~*") else ("exact" if mod == "=" else "prefix"),
                      "pat": pat, "proxy": pm.group(1).strip()})
    rules.sort(key=lambda r: r["pos"])
    return rules


def port_of(target: str, ports: dict[str, int]) -> int:
    if "grape_backend" in target:
        return ports.get("mgmt", 8081)
    m = re.search(r":(\d{2,5})", target)
    return int(m.group(1)) if m else -1


def pick(path: str, rules: list[dict], ports: dict[str, int]) -> tuple[int, str]:
    for r in rules:
        if r["kind"] == "regex" and re.search(r["pat"], path):
            return port_of(r["proxy"], ports), r["pat"]
    for r in rules:
        if r["kind"] == "exact" and path == r["pat"]:
            return port_of(r["proxy"], ports), r["pat"]
    best = None
    for r in rules:
        if r["kind"] == "prefix" and path.startswith(r["pat"]):
            if best is None or len(r["pat"]) > len(best["pat"]):
                best = r
    return (port_of(best["proxy"], ports), best["pat"]) if best else (-1, "无规则")


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", default=os.environ.get("GRAPE_NGINX_CONF", DEFAULT_CONF))
    ap.add_argument("--public", default="", help="公网基址，附加全量 OPTIONS 可达性检查")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ports = _ports()
    routes = {role: fetch_routes(port) for role, port in ports.items()}
    alive = {r: len(v) for r, v in routes.items()}
    rules = parse_nginx(args.conf)

    problems: list[dict] = []
    if not rules:
        problems.append({"kind": "no-nginx-conf", "detail": f"未解析到规则（{args.conf} 不存在或为空）"})

    all_paths = sorted({p for rs in routes.values() for p in rs})
    for path in all_paths:
        target, why = pick(path, rules, ports)
        if target == -1:
            problems.append({"kind": "no-rule", "path": path, "why": why})
            continue
        role = next((r for r, p in ports.items() if p == target), "?")
        declared = routes.get(role, {}).get(path, set())
        if not declared:
            # 该进程 schema 未声明 → 用实际请求判定（有的路由 include_in_schema=False）
            code, _ = _http(f"http://127.0.0.1:{target}{concrete(path)}", "OPTIONS")
            if code == 404:
                owners = sorted(r for r, rs in routes.items() if path in rs)
                problems.append({"kind": "drift", "path": path, "nginx_to": target,
                                 "role": role, "owners": owners, "why": why})

    public_problems = []
    if args.public:
        base = args.public.rstrip("/")
        for path in all_paths:
            code, _ = _http(base + concrete(path), "OPTIONS", timeout=10)
            if code == 404:
                public_problems.append({"kind": "public-404", "path": path})

    report = {
        "ports": ports,
        "alive_routes": alive,
        "nginx_rules": len(rules),
        "checked_paths": len(all_paths),
        "problems": problems,
        "public_problems": public_problems,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"进程路由量: {alive}   nginx 规则: {len(rules)}   受检路径: {len(all_paths)}")
        if not problems and not public_problems:
            print("RESULT: PASS —— nginx 分流与进程路由一致")
        for p in problems:
            print(f"  [漂移] {p}")
        for p in public_problems:
            print(f"  [公网404] {p}")

    return 1 if (problems or public_problems) else 0


if __name__ == "__main__":
    sys.exit(main())