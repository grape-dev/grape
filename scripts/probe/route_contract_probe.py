#!/usr/bin/env python3
"""四进程路由契约探针：nginx 分流 vs 各进程实际路由。

背景（2026-11-12 事故）：切四进程后 nginx 未同步 ——
  · /api/events/{seq}/(a2a-redo|chat|knowledge|flow/reset) 被整组代理到事件进程，
    实际只注册在 mgmt → 404（A2A 补发失败）
  · POST /api/pipeline/handoff 无规则，被 `/` 兜底到 mgmt（只有 GET）→ 405

本探针把「nginx 会把这条路径送到哪个进程」与「那个进程是否真的服务它」对齐，
任何不一致即失败（退出码 1），供 CI / 部署后自检使用。

判定口径（2026-11-12 修订，回应 SRE/QA 审查）：
  · mgmt 对**未知路径的 OPTIONS 也返回 405**（实测 /api/zzz-nope → 405），
    所以不能用 OPTIONS 判存在性 —— 改用 **GET**：未知路径恒 404，
    已注册路径返回 200/401/405/422 之一。全部 GET 探测，无副作用。
  · proxy_pass 支持 upstream 名（解析 upstream 块取 :port），因此仓库模板
    deploy/nginx-grape-four-process.conf 可直接作为 --conf 输入。
  · nginx 匹配语义：`=` 精确优先 → `^~` 最长前缀（跳过 regex）→
    regex 按出现顺序（`~*` 大小写不敏感）→ 普通前缀最长。
  · 目标端口不可达 → 记为 unreachable（fail-loud），不算 PASS。

用法：
    python3 scripts/probe/route_contract_probe.py
    python3 scripts/probe/route_contract_probe.py --public https://grape-dev.topxtopx.com
    python3 scripts/probe/route_contract_probe.py --conf deploy/nginx-grape-four-process.conf --json

环境变量：
    GRAPE_PROBE_PORTS  "mgmt=8081,event=8082,worker=8083,insight=8084"（按角色定位本机端口）
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
UPSTREAM_ROLE = {
    "grape_server": "mgmt", "grape_backend": "mgmt",
    "grape_event_server": "event", "grape_worker_server": "worker",
    "grape_insight_server": "insight",
}


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


def _http(url: str, method: str = "GET", timeout: float = 30.0, headers: dict | None = None):
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("allow", "")
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("allow", "") if e.headers else "")
    except Exception as e:
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


def _braces(text: str, start: int) -> int:
    """从 start 处的 '{' 起做括号配对，返回匹配 '}' 的下标。"""
    depth, j = 0, start
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(text) - 1


def parse_nginx(conf: str) -> tuple[list[dict], dict[str, int]]:
    """解析 location 规则与 upstream 块。

    返回 (rules, upstreams)。rules 保持文件出现顺序，用于 regex 先后判定。
    """
    try:
        text = open(conf, encoding="utf-8").read()
    except OSError:
        return [], {}

    upstreams: dict[str, int] = {}
    for m in re.finditer(r"upstream\s+([\w.-]+)\s*\{", text):
        name = m.group(1)
        body = text[m.end(): _braces(text, m.end() - 1)]
        sm = re.search(r"server\s+([\w.\-\[\]]+):(\d+)", body)
        if sm:
            upstreams[name] = int(sm.group(2))

    rules = []
    for m in re.finditer(r"location\s+(~\*|~|\^~|=)?\s*(\S+)\s*\{", text):
        mod = (m.group(1) or "").strip()
        pat = m.group(2)
        body = text[m.end(): _braces(text, m.end() - 1)]
        pm = re.search(r"proxy_pass\s+([^;]+);", body)
        if not pm:
            continue
        if mod == "=":
            kind = "exact"
        elif mod == "~":
            kind = "regex"
        elif mod == "~*":
            kind = "regex_ci"
        elif mod == "^~":
            kind = "prefix_noregex"
        else:
            kind = "prefix"
        rules.append({"pos": m.start(), "kind": kind, "pat": pat,
                      "proxy": pm.group(1).strip()})
    rules.sort(key=lambda r: r["pos"])
    return rules, upstreams


def port_of(target: str, ports: dict[str, int], upstreams: dict[str, int]) -> int:
    t = target.strip()
    m = re.search(r"://([\w.-]+)", t)
    name = m.group(1) if m else t.split("/")[0]
    if name in upstreams:
        return upstreams[name]
    role = UPSTREAM_ROLE.get(name)
    if role:
        return ports.get(role, -1)
    pm = re.search(r":(\d{2,5})", t)
    return int(pm.group(1)) if pm else -1


def pick(path: str, rules: list[dict], ports: dict[str, int],
         upstreams: dict[str, int]) -> tuple[int, str]:
    """按 nginx 语义选出目标端口：exact → ^~ 最长前缀 → regex 顺序 → 前缀最长。"""
    for r in rules:
        if r["kind"] == "exact" and path == r["pat"]:
            return port_of(r["proxy"], ports, upstreams), f"exact {r['pat']}"

    norx = None
    for r in rules:
        if r["kind"] == "prefix_noregex" and path.startswith(r["pat"]):
            if norx is None or len(r["pat"]) > len(norx["pat"]):
                norx = r
    if norx is not None:
        return port_of(norx["proxy"], ports, upstreams), f"^~ {norx['pat']}"

    for r in rules:
        if r["kind"] in ("regex", "regex_ci"):
            flags = re.IGNORECASE if r["kind"] == "regex_ci" else 0
            if re.search(r["pat"], path, flags):
                return port_of(r["proxy"], ports, upstreams), f"regex {r['pat']}"

    best = None
    for r in rules:
        if r["kind"] == "prefix" and path.startswith(r["pat"]):
            if best is None or len(r["pat"]) > len(best["pat"]):
                best = r
    if best is not None:
        return port_of(best["proxy"], ports, upstreams), f"prefix {best['pat']}"
    return -1, "无规则"


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", default=os.environ.get("GRAPE_NGINX_CONF", DEFAULT_CONF))
    ap.add_argument("--public", default="", help="公网基址，附加全量可达性检查")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--warmup", action="store_true",
                    help="先预热重接口（worker 包 / 安装包），避免首次现场打包把探测拖成假红")
    args = ap.parse_args()

    ports = _ports()
    if args.warmup:
        heavy = ("/api/runtime/grape-worker.tar.gz", "/api/runtime/install/package")
        for role, port in ports.items():
            for hp in heavy:
                _http(f"http://127.0.0.1:{port}{hp}", "GET", timeout=60.0,
                      headers={"Range": "bytes=0-0"})
        print("[warmup] 已预热重接口（worker 包 / 安装包）")

    routes = {role: fetch_routes(port) for role, port in ports.items()}
    alive = {r: len(v) for r, v in routes.items()}
    rules, upstreams = parse_nginx(args.conf)

    # 端口 → 角色：环境变量与 conf upstream 名双源合成。
    # 两源对同一端口给出不同角色 = 部署口径错配（QA #1272 M3），单条报出后退出，
    # 否则逐路径会刷成 unreachable/drift 噪音，把真实漂移淹没。
    port_role = {p: r for r, p in ports.items()}
    config_conflicts = []
    for name, port in (upstreams or {}).items():
        role = UPSTREAM_ROLE.get(name)
        if not role:
            continue
        prev = port_role.get(port)
        if prev and prev != role:
            config_conflicts.append({"kind": "config-mismatch", "port": port,
                                     "by_env": prev, "by_upstream": role, "upstream": name})
        else:
            port_role.setdefault(port, role)

    problems: list[dict] = []
    if not rules:
        problems.append({"kind": "no-nginx-conf",
                         "detail": f"未解析到规则（{args.conf} 不存在或为空）"})

    # 存活断言（QA #1272 M2）：四角色任一取不到 spec（进程没起/端口不通）都必须失败。
    # 否则 all_paths 为空 → 无 problem → 输出 PASS，新接线的门禁等于恒绿。
    for role, rs in routes.items():
        if not rs:
            problems.append({"kind": "liveness",
                             "detail": f"{role} 进程未取到路由表（端口 {ports.get(role)} 未启动或不可达）",
                             "role": role, "port": ports.get(role)})

    all_paths = sorted({p for rs in routes.values() for p in rs})
    if not all_paths:
        problems.append({"kind": "liveness", "detail": "四进程路由表全为空，探针无法判定"})
    probe_cache: dict[tuple[int, str], int] = {}

    def exists_on(port: int, path: str) -> int:
        key = (port, path)
        if key not in probe_cache:
            # Range: bytes=0-0 → 存在返回 206/200/401/405，未知路径仍 404；
            # 避免 /api/runtime/grape-worker.tar.gz 这类大文件把 GET 拖到超时（QA #1272 复核）。
            # 现场打包类接口（如 /api/runtime/grape-worker.tar.gz）首次访问要现打压缩包，
            # 部署刚重启时会 >8s；旧默认 8s 会把"慢"误判成 unreachable（假红）。
            code, _ = _http(f"http://127.0.0.1:{port}{concrete(path)}", "GET",
                            timeout=30.0, headers={"Range": "bytes=0-0"})
            probe_cache[key] = code
        return probe_cache[key]

    for path in all_paths:
        target, why = pick(path, rules, ports, upstreams)
        if target == -1:
            problems.append({"kind": "no-rule", "path": path, "why": why})
            continue
        role = port_role.get(target, "?")
        owners = sorted(r for r, rs in routes.items() if path in rs)
        if owners:
            if role in owners:
                continue
            # 归属不符 → 再用 Range-GET 实测目标进程是否其实持有该路径
            # （mgmt 有一批 include_in_schema=False 的 GET-only 兜底路由，
            #  例：/api/runtime/install/package 由 worker 声明、mgmt 亦可服务）。
            code = exists_on(target, path)
            if code == -1:
                problems.append({"kind": "unreachable", "path": path, "nginx_to": target,
                                 "role": role, "why": why})
            elif code == 404:
                problems.append({"kind": "drift", "path": path, "nginx_to": target,
                                 "role": role, "owners": owners, "why": why})
            continue
        # 说明：all_paths 取自各进程 spec 并集，hidden 路由（include_in_schema=False）
        # 不会进入受检集合，故此处无需再判 hidden 分支（QA #1272 I1 指出的死代码）。

    public_problems = []
    if args.public:
        base = args.public.rstrip("/")
        for path in all_paths:
            methods = {m for rs in routes.values() if path in rs for m in rs[path]}
            if methods and "GET" not in methods:
                continue  # POST/PUT-only：GET 会被 SPA 兜底成 404，不可判，跳过
            code, _ = _http(base + concrete(path), "GET", timeout=10,
                            headers={"Range": "bytes=0-0"})
            if code == 404:
                public_problems.append({"kind": "public-404", "path": path})

    problems.extend(config_conflicts)

    report = {
        "ports": ports,
        "upstreams": upstreams,
        "alive_routes": alive,
        "nginx_rules": len(rules),
        "checked_paths": len(all_paths),
        "problems": problems,
        "public_problems": public_problems,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"进程路由量: {alive}   upstream: {upstreams or '(无)'}   "
              f"nginx 规则: {len(rules)}   受检路径: {len(all_paths)}")
        if not problems and not public_problems:
            print("RESULT: PASS —— nginx 分流与进程路由一致")
        for p in problems:
            print(f"  [{p['kind']}] {p}")
        for p in public_problems:
            print(f"  [公网404] {p}")

    return 1 if (problems or public_problems) else 0


if __name__ == "__main__":
    sys.exit(main())