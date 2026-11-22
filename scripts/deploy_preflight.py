#!/usr/bin/env python3
"""部署前自检（平台对象四表 / 迁移台账 / 进程角色 / 契约登记表）。

背景（2026-11-16 实弹）：一次部署把迁移台账盖了章（202611170030 duration 2 ms），
但四张平台对象表没有落地——PAT 保存钩子随后写 `grape_t_user_repo_platform_link`
报 `no such table`，表现为「配了 PAT 但平台关系不出现」，而接口层全是 200，人工难发现。
另一个同源坑：进程没带 `GRAPE_PROCESS_ROLE` 时，表归属守卫按「非 owner」把四表 DDL
整批跳过，症状一模一样。

本脚本在起服务**之前**把这两件事查清楚：台账↔注册表对账、owner 表实存、角色 env。
退出码：0 全通过；1 有未通过项（不得启动）；2 环境/参数错误。

用法：
    python3 scripts/deploy_preflight.py                      # 读当前 env 的库
    python3 scripts/deploy_preflight.py --db-path data/sessions.db --role mgmt
    python3 scripts/deploy_preflight.py --json               # 机器可读
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Issue #3756：必需契约文件清单的单一事实源（与 release_pack.py 共用）。
from src.domains.contracts.registry_files import (  # noqa: E402
    CONTRACTS_DIR,
    REQUIRED_REGISTRY_FILES,
    REQUIRED_RELEASED_GLOB,
)

DOMAINS = ("mgmt", "runtime", "insight")

# 每个域必须存在的表（本轮只收口 D109 平台对象 + 元数据表；后续可扩）
REQUIRED_TABLES: dict[str, tuple[str, ...]] = {
    "mgmt": (
        "grape_t_oauth_channels",
        "grape_t_repo_platforms",
        "grape_t_channel_repo_link",
        "grape_t_user_repo_platform_link",
    ),
    "runtime": ("meta",),
    "insight": ("grape_t_repo_credentials",),
}

LEDGER = "grape_t_schema_migrations"

# 网关上游端口单一事实源（Issue #3764）：与 deploy/nginx-grape-cluster.conf、
# scripts/restart-four-process.sh、scripts/install-grape-systemd.sh 的四进程布局一致。
# 其余脚本/配置只引用，不复制；001 过渡口径（如 8081–8084）用 --ports /
# GRAPE_PROCESS_PORTS 覆盖。
UPSTREAM_ROLE_PORT = {"mgmt": 38081, "event": 38082, "worker": 38083, "insight": 38084}

# location 前缀 → 期望进程角色。/api/runtime 为硬失败项，其余仅 warn。
ROLE_LOCATION_HINTS: tuple[tuple[str, str], ...] = (
    ("/api/runtime", "worker"),
    ("/api/tasks", "worker"),
    ("/api/webhook", "event"),
    ("/api/a2a", "event"),
    ("/api/dashboard", "insight"),
    ("/api/obs", "insight"),
    ("/api/search", "insight"),
    ("/api/latency", "insight"),
)


class Check:
    def __init__(self, name: str):
        self.name = name
        self.ok = True
        self.detail = ""
        self.hint = ""

    def fail(self, detail: str, hint: str = "") -> "Check":
        self.ok = False
        self.detail = detail
        self.hint = hint
        return self

    def as_dict(self) -> dict:
        return {"check": self.name, "ok": self.ok, "detail": self.detail, "hint": self.hint}


def _split_topology() -> bool:
    """三库拓扑：三个域 DSN 都配了才算（单库/测试回落进程库）。"""
    return all(os.environ.get(f"GRAPE_DSN_{d.upper()}", "").strip() for d in DOMAINS)


def _open_db(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def check_role(role: str) -> Check:
    c = Check("role_env")
    if not role:
        return c.fail(
            "进程角色未设置（GRAPE_PROCESS_ROLE / DEVOPS_DB_NAME 都空）",
            "缺角色时表归属守卫按「非 owner」跳过本域 DDL → 平台四表不会建、PAT 关系写不进。"
            "请给每个进程写死角色：mgmt / runtime / insight（四个启动入口已 setdefault，"
            "自定义启动方式需显式 export）。")
    if role not in DOMAINS:
        return c.fail(f"角色非法: {role!r}（应为 {DOMAINS} 之一）")
    c.detail = role
    return c


def check_contracts_registry(contracts_dir: str) -> Check:
    """Issue #3756：契约登记表就位自检——缺件时 /openapi.json 会整体 500。

    必需文件清单与打包断言共用 `src.domains.contracts.registry_files`：缺
    `openapi_baseline.json` / `required_operations.json` 时完整性门禁同样
    fail-fast，预检必须在启动前拦下，不能只查弃用登记表两件。
    """
    c = Check("contracts_registry")
    root = Path(contracts_dir)
    missing: list[str] = []
    invalid: list[str] = []
    for name in REQUIRED_REGISTRY_FILES:
        p = root / name
        if not p.is_file():
            missing.append(name)
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 预检要带出损坏原因
            invalid.append(f"{name}: {exc}")
    released = sorted((root / "released").glob("*.json")) if (root / "released").is_dir() else []
    if missing or invalid or not released:
        parts = []
        if missing:
            parts.append(f"缺文件 {missing}")
        if invalid:
            parts.append("损坏 " + "; ".join(invalid))
        if not released:
            parts.append("released/ 无 *.json 发布快照")
        return c.fail(
            f"契约登记表不就位（{root}）: " + "，".join(parts),
            "发布产物须包含 " + CONTRACTS_DIR + "/ 整目录（"
            + " / ".join(REQUIRED_REGISTRY_FILES) + " / " + REQUIRED_RELEASED_GLOB
            + "）。请确认打包基线并重新打包/同步；缺登记表时 /openapi.json "
            "会降级或整体不可用。")
    c.detail = f"{root}（released={len(released)}）"
    return c


def check_db(path: str) -> tuple[Check, sqlite3.Connection | None]:
    c = Check("db_reachable")
    if not path or not Path(path).is_file():
        return c.fail(f"数据库文件不存在: {path!r}", "检查 DEVOPS_DB_PATH / --db-path"), None
    try:
        conn = _open_db(path)
        conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - 预检要把原因原样带出
        return c.fail(f"数据库不可读: {exc}"), None
    c.detail = path
    return c, conn


def check_ledger(conn: sqlite3.Connection) -> tuple[Check, dict]:
    c = Check("migration_ledger")
    try:
        rows = conn.execute(f"SELECT version, name FROM {LEDGER}").fetchall()
    except Exception as exc:  # noqa: BLE001 - 缺表即首跑库
        return c.fail(f"迁移台账表缺失（{LEDGER}）: {exc}",
                      "空库首跑：请先起 runtime 进程（meta 归 runtime），再起 mgmt。"), {}
    if not rows:
        return c.fail(f"迁移台账为空（{LEDGER} 0 行）",
                      "空库首跑：请先起 runtime 进程（meta 归 runtime），再起 mgmt。"), {}
    stamped = {v: n for v, n in rows}
    c.detail = f"{len(stamped)} rows, max={max(stamped)}"
    return c, stamped


def check_registry_alignment(stamped: dict) -> Check:
    c = Check("ledger_vs_registry")
    from src.api import db_setup

    registry = {m.version: m.name for m in db_setup._MIGRATIONS}
    collisions = [
        f"{v}: 库内={stamped[v]!r} 代码={registry[v]!r}"
        for v in sorted(set(stamped) & set(registry))
        if stamped[v] and stamped[v] != registry[v]
    ]
    if collisions:
        return c.fail("同号不同名（撞号，该版本会被判「已执行」永久跳过）: " + "; ".join(collisions),
                      "把本分支迁移 renumber 到未占用号后重来。")
    pending = [v for v in registry if v not in stamped]
    c.detail = (f"registered={len(registry)} pending={len(pending)}"
                + (f" ({', '.join(sorted(pending)[:4])}{'…' if len(pending) > 4 else ''})"
                   if pending else ""))
    return c


def check_tables(conn: sqlite3.Connection, role: str, split: bool) -> Check:
    c = Check("owned_tables_exist")
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    need: list[str] = []
    if not split:
        # 单库：跨域读回落进程库，meta 必须在同一份库里
        need.append("meta")
    need += [t for t in REQUIRED_TABLES.get(role, ()) if t not in need]
    missing = [t for t in need if t not in have]
    if missing:
        return c.fail(f"owner 表缺失: {missing}",
                      "台账可能已盖章而 DDL 未落地（2026-11-16 实锤）。先跑一次启动让 "
                      "202611170041 ensure-platform-object-tables 自愈，或人工重放 "
                      "src/api/platform_object_ddl.py 的四表 DDL。")
    c.detail = f"{len(need)} 张: " + ", ".join(need)
    return c


@dataclass
class UpstreamServer:
    host: str
    port: int
    line: int


@dataclass
class NginxLocation:
    modifier: str
    pattern: str
    proxies: list[tuple[str, int]]
    line: int


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _find_block(text: str, open_pos: int) -> tuple[str, int]:
    """从 `{` 之后开始做花括号配对，返回 (块内文本, 块内文本绝对起点)。"""
    depth = 1
    i = open_pos
    while i < len(text) and depth:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return text[open_pos:i - 1], open_pos


def _split_host_port(token: str) -> tuple[str, int | None]:
    token = token.strip()
    if not token or token.startswith("unix:"):
        return token, None
    if token.startswith("["):
        end = token.find("]")
        if end == -1:
            return token, None
        host = token[1:end]
        rest = token[end + 1:]
        if rest.startswith(":"):
            try:
                return host, int(rest[1:])
            except ValueError:
                return host, None
        return host, None
    if ":" in token:
        host, _, port = token.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return token, None
    return token, None


def parse_upstreams(text: str) -> dict[str, list[UpstreamServer]]:
    """解析 nginx `upstream <name> { server host:port; ... }` 块。"""
    upstreams: dict[str, list[UpstreamServer]] = {}
    for m in re.finditer(r"\bupstream\s+([A-Za-z0-9_.-]+)\s*\{", text):
        body, body_start = _find_block(text, m.end())
        servers: list[UpstreamServer] = []
        for sm in re.finditer(r"\bserver\s+([^;]+);", body):
            token = sm.group(1).split()[0]
            host, port = _split_host_port(token)
            if port is not None:
                servers.append(UpstreamServer(host, port, _line_of(text, body_start + sm.start())))
        upstreams[m.group(1)] = servers
    return upstreams


def parse_locations(text: str) -> list[NginxLocation]:
    """解析 nginx `location [modifier] pattern { proxy_pass ...; }`。"""
    locations: list[NginxLocation] = []
    pattern = re.compile(r"\blocation\s+(?:([=]|\^~|~\*|~)\s+)?([^\s{]+)\s*\{")
    for m in pattern.finditer(text):
        body, body_start = _find_block(text, m.end())
        proxies = [
            (pm.group(1).strip(), _line_of(text, body_start + pm.start()))
            for pm in re.finditer(r"\bproxy_pass\s+([^;]+);", body)
        ]
        locations.append(NginxLocation(
            m.group(1) or "", m.group(2), proxies, _line_of(text, m.start())))
    return locations


def _normalized_pattern(loc: NginxLocation) -> str:
    p = loc.pattern
    if p.startswith("^"):
        p = p[1:]
    if p.startswith("~"):
        p = p[1:]
    if p.startswith("*"):
        p = p[1:]
    return p


def _is_runtime_location(loc: NginxLocation) -> bool:
    return _normalized_pattern(loc).startswith("/api/runtime")


def _location_role(loc: NginxLocation) -> str | None:
    p = _normalized_pattern(loc)
    for key, role in ROLE_LOCATION_HINTS:
        if p.startswith(key):
            return role
    return None


def _resolve_proxy(raw: str, upstreams: dict[str, list[UpstreamServer]]
                   ) -> tuple[list[UpstreamServer] | None, str]:
    """把 proxy_pass 目标解析为上游 server 列表；返回 (servers, upstream 名/地址)。"""
    m = re.match(r"https?://([^/;]+)", raw)
    if not m:
        return None, raw
    authority = m.group(1)
    if authority in upstreams:
        return upstreams[authority], authority
    host, port = _split_host_port(authority)
    if port is None:
        return None, authority
    return [UpstreamServer(host, port, 0)], authority


def check_upstream_role(conf_text: str | None, ports: dict[str, int] | None = None,
                        source: str = "") -> Check:
    """断言 /api/runtime* 的 proxy_pass 上游端口归属 worker。

    未提供配置时 skip（ok=True），避免误伤无网关节点；配置中无 /api/runtime*
    时同样 skip。错配或上游无法解析时 fail，detail 含 upstream 地址端口、
    期望端口与 conf 行号，main() 据此返回非 0。
    """
    c = Check("nginx_upstream_role")
    if not conf_text or not conf_text.strip():
        c.detail = "skip: 未提供/未探测到 nginx 配置"
        return c
    role_ports = {**UPSTREAM_ROLE_PORT, **(ports or {})}
    upstreams = parse_upstreams(conf_text)
    locations = parse_locations(conf_text)
    runtime_locs = [loc for loc in locations if _is_runtime_location(loc)]
    if not runtime_locs:
        c.detail = "skip: 配置中未发现 /api/runtime* location"
        return c

    expected = role_ports["worker"]
    problems: list[str] = []
    warnings: list[str] = []
    for loc in runtime_locs:
        for raw, line in loc.proxies:
            servers, name = _resolve_proxy(raw, upstreams)
            if servers is None:
                problems.append(
                    f"{loc.pattern} (L{loc.line}) proxy_pass {raw} (L{line}) "
                    "无法解析上游（upstream 未定义或地址缺少端口）")
                continue
            for srv in servers:
                if srv.port != expected:
                    where = f"server L{srv.line}" if srv.line else f"L{line}"
                    problems.append(
                        f"{loc.pattern} (L{loc.line}) upstream={name} "
                        f"server {srv.host}:{srv.port} ({where}) 期望 worker:{expected}")

    for loc in locations:
        if _is_runtime_location(loc):
            continue
        role = _location_role(loc)
        if role is None:
            continue
        exp = role_ports[role]
        for raw, line in loc.proxies:
            servers, name = _resolve_proxy(raw, upstreams)
            if servers is None:
                warnings.append(f"{loc.pattern} (L{loc.line}) proxy_pass {raw} 无法解析上游")
                continue
            for srv in servers:
                if srv.port != exp:
                    warnings.append(
                        f"{loc.pattern} (L{loc.line}) upstream={name} "
                        f"{srv.host}:{srv.port} → 期望 {role}:{exp}")

    if problems:
        return c.fail(
            "; ".join(problems),
            "上游端口必须归属对应进程：/api/runtime* → worker。请对齐 "
            "deploy/nginx-grape-cluster.conf（入口 38080；mgmt/event/worker/insight="
            "38081/38082/38083/38084）；001 过渡口径可用 --ports 或 "
            "GRAPE_PROCESS_PORTS 覆盖（如 worker=8083）。")
    c.detail = f"{len(runtime_locs)} 个 /api/runtime* location 归属 worker:{expected}"
    if warnings:
        c.detail += "；warn: " + "; ".join(warnings)
    return c


def parse_ports(spec: str | None) -> dict[str, int]:
    """解析 --ports / GRAPE_PROCESS_PORTS，如 `worker=8083` 或 `mgmt=8081,event=8082`。"""
    ports: dict[str, int] = {}
    if not spec or not spec.strip():
        return ports
    for part in re.split(r"[,;\s]+", spec.strip()):
        if not part:
            continue
        if "=" in part:
            role, value = part.split("=", 1)
        elif ":" in part:
            role, value = part.split(":", 1)
        else:
            raise ValueError(f"--ports/GRAPE_PROCESS_PORTS 项格式应为 role=port: {part!r}")
        role = role.strip().lower()
        if role not in UPSTREAM_ROLE_PORT:
            raise ValueError(
                f"未知进程角色: {role!r}（可选 {', '.join(UPSTREAM_ROLE_PORT)}）")
        try:
            ports[role] = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"端口非整数: {part!r}") from exc
    return ports


def load_nginx_text(conf_path: str | None = None, dump_path: str | None = None
                    ) -> tuple[str | None, str]:
    """读取 nginx 配置：显式 conf / nginx -T 转储 > GRAPE_NGINX_CONF > 常见探测路径。"""
    if conf_path:
        return Path(conf_path).read_text(encoding="utf-8", errors="replace"), f"conf:{conf_path}"
    if dump_path:
        return Path(dump_path).read_text(encoding="utf-8", errors="replace"), f"dump:{dump_path}"
    env_path = (os.environ.get("GRAPE_NGINX_CONF") or "").strip()
    if env_path:
        return Path(env_path).read_text(encoding="utf-8", errors="replace"), f"conf:{env_path}"
    for cand in sorted(glob.glob("/etc/nginx/conf.d/grape*.conf")):
        try:
            return Path(cand).read_text(encoding="utf-8", errors="replace"), f"conf:{cand}"
        except OSError:
            continue
    return None, ""


def run(db_path: str, role: str, contracts_dir: str | None = None, *,
        nginx_text: str | None = None,
        ports: dict[str, int] | None = None, nginx_source: str = "") -> list[Check]:
    checks: list[Check] = [
        check_role(role),
        check_contracts_registry(contracts_dir or str(REPO / "contracts")),
    ]
    db_check, conn = check_db(db_path)
    checks.append(db_check)
    checks.append(check_upstream_role(nginx_text, ports=ports, source=nginx_source))
    if conn is None:
        return checks
    split = _split_topology()
    ledger_check, stamped = check_ledger(conn)
    checks.append(ledger_check)
    checks.append(check_registry_alignment(stamped))
    checks.append(check_tables(conn, role, split))
    conn.close()
    return checks


def main() -> int:
    ap = argparse.ArgumentParser(description="Grape deploy preflight")
    ap.add_argument("--db-path", default=os.environ.get("DEVOPS_DB_PATH", ""),
                    help="sqlite 文件路径（默认取 DEVOPS_DB_PATH）")
    ap.add_argument("--role", default=(os.environ.get("GRAPE_PROCESS_ROLE") or "").strip().lower())
    ap.add_argument("--contracts-dir",
                    default=os.environ.get("GRAPE_CONTRACTS_DIR") or str(REPO / "contracts"),
                    help="契约登记表目录（默认取 GRAPE_CONTRACTS_DIR 或 REPO/contracts）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--nginx-conf", default="",
                    help="nginx 配置文件路径（/api/runtime* 上游端口归属断言用）")
    ap.add_argument("--nginx-dump", default="",
                    help="`nginx -T` 转储文件路径（与 --nginx-conf 等价）")
    ap.add_argument("--ports", default=os.environ.get("GRAPE_PROCESS_PORTS", ""),
                    help="覆盖端口事实源，如 worker=8083 或 mgmt=8081,event=8082,...")
    args = ap.parse_args()

    if os.environ.get("DEVOPS_DB_BACKEND", "sqlite").lower() == "mysql":
        print("preflight: mysql 后端暂走 deploy_mysql_gate.py（本脚本覆盖 sqlite 单库/测试拓扑）",
              file=sys.stderr)
        return 2
    try:
        ports = parse_ports(args.ports)
    except ValueError as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 2
    try:
        nginx_text, nginx_source = load_nginx_text(
            args.nginx_conf or None, args.nginx_dump or None)
    except OSError as exc:
        print(f"preflight: 读取 nginx 配置失败: {exc}", file=sys.stderr)
        return 2
    db_path = args.db_path or str(REPO / "data" / "sessions.db")
    checks = run(db_path, args.role, args.contracts_dir, nginx_text=nginx_text,
                 ports=ports, nginx_source=nginx_source)
    failed = [c for c in checks if not c.ok]

    if args.json:
        print(json.dumps({"db": db_path, "role": args.role, "nginx_source": nginx_source,
                          "pass": not failed, "checks": [c.as_dict() for c in checks]},
                         ensure_ascii=False, indent=2))
    else:
        for c in checks:
            print(f"{'PASS' if c.ok else 'FAIL'}  {c.name:22s} {c.detail}")
            if not c.ok and c.hint:
                print(f"      ↳ {c.hint}")
        print(f"\n{'PASS' if not failed else 'FAIL'}: {len(checks) - len(failed)}/{len(checks)} 项通过"
              f"（db={db_path}, role={args.role or '∅'}）")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())