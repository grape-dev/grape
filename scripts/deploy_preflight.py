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
import json
import os
import sqlite3
import sys
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


def run(db_path: str, role: str, contracts_dir: str | None = None) -> list[Check]:
    checks: list[Check] = [
        check_role(role),
        check_contracts_registry(contracts_dir or str(REPO / "contracts")),
    ]
    db_check, conn = check_db(db_path)
    checks.append(db_check)
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
    args = ap.parse_args()

    if os.environ.get("DEVOPS_DB_BACKEND", "sqlite").lower() == "mysql":
        print("preflight: mysql 后端暂走 deploy_mysql_gate.py（本脚本覆盖 sqlite 单库/测试拓扑）",
              file=sys.stderr)
        return 2
    db_path = args.db_path or str(REPO / "data" / "sessions.db")
    checks = run(db_path, args.role, args.contracts_dir)
    failed = [c for c in checks if not c.ok]

    if args.json:
        print(json.dumps({"db": db_path, "role": args.role, "pass": not failed,
                          "checks": [c.as_dict() for c in checks]}, ensure_ascii=False, indent=2))
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