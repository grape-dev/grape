#!/usr/bin/env python3
"""阶段环境 API 冒烟：在"视图已清零"的三独立库上跑真实读路径，暴露剩余跨域 SQL。

背景：三库独立化后非归属库不再有跨域视图，任何仍直读外域表的代码都会 1146/1054。
本工具用 in-process TestClient（覆盖鉴权依赖）遍历一组只读端点，把
`Table ... doesn't exist` / `Unknown column` 等失败逐条打出来——这是
P1/P3.5 剩余工作**按真实执行路径**排序的依据（比静态引用数更准）。

前置：先跑 `scripts/rds_stage_rehearsal.py` 生成 stage 库与 `~/.grape-rds-stage.env`。

用法：
  GRAPE_DOMAIN_LIB=1 python3 scripts/stage_api_smoke.py            # 内置只读端点
  python3 scripts/stage_api_smoke.py --all                          # 自动发现所有无参 GET
  python3 scripts/stage_api_smoke.py --json                         # 机器可读输出

退出码：0 = 无 5xx；1 = 存在 5xx（逐条列出端点与错误摘要）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
ENV_FILE = Path.home() / ".grape-rds-stage.env"

# 内置只读端点：覆盖列表页/详情页的主要数据来源（skills/repos/agents/scenes/tasks/events/chat/看板）
READONLY_ENDPOINTS = [
    "/api/ping",
    "/api/version",
    "/api/skills",
    "/api/skills/categories",
    "/api/repos",
    "/api/agents",
    "/api/scenes",
    "/api/scenes/market",
    "/api/tasks",
    "/api/events",
    "/api/chat/conversations",
    "/api/orgs",
    "/api/admin/users",
    "/api/admin/agents",
    "/api/dashboard/overview",
    "/api/settings/event-action-filters",
]


def load_stage_env(db_user: str = "", db_password: str = "") -> dict[str, str]:
    """读 stage DSN 文件；把 mgmt DSN 同时映射为进程主库（DEVOPS_DB_*）。"""
    env: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k] = v
    mgmt = env.get("GRAPE_DSN_MGMT", "")
    if mgmt:
        u = urlparse(mgmt)
        env.update({
            "DEVOPS_DB_BACKEND": "mysql",
            "DEVOPS_DB_HOST": u.hostname or "127.0.0.1",
            "DEVOPS_DB_PORT": str(u.port or 3306),
            # 启动期迁移需要 DDL：主库连接用 --db-user/--db-password（默认 root），
            # GRAPE_DSN_* 仍用只授本库的应用账号（这正是要验证的权限面）。
            "DEVOPS_DB_USER": db_user or u.username or "root",
            "DEVOPS_DB_PASSWORD": db_password if db_password else (u.password or ""),
            "DEVOPS_DB_NAME": (u.path or "/").lstrip("/"),
        })
    return env


def discover_get_paths(app) -> list[str]:
    from src.api.openapi import _iter_routes

    out: list[str] = []
    for route in app.routes:
        for r in _iter_routes(route):
            path = getattr(r, "path", "")
            methods = getattr(r, "methods", set()) or set()
            if "GET" not in methods or not path.startswith("/api/") or "{" in path:
                continue
            out.append(path)
    return sorted(set(out))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="stage API smoke（视图清零环境）")
    ap.add_argument("--all", action="store_true", help="自动发现所有无路径参数的 /api/* GET")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--db-user", default=os.environ.get("MYSQL_USER", "root"),
                    help="主库连接账号（需 DDL；默认 root）")
    ap.add_argument("--db-password", default=os.environ.get("MYSQL_PWD", ""))
    args = ap.parse_args(argv)

    env = load_stage_env(args.db_user, args.db_password)
    if not env.get("GRAPE_DSN_RUNTIME"):
        print(f"[FATAL] 未找到 stage DSN（{ENV_FILE}）；先运行 scripts/rds_stage_rehearsal.py",
              file=sys.stderr)
        return 2
    os.environ.update(env)
    os.environ.setdefault("GRAPE_DOMAIN_LIB", "1")
    os.environ.setdefault("GRAPE_SERVICE_NAME", "mgmt")

    from fastapi.testclient import TestClient

    from src.api import deps
    from src.api.main import app

    fake_user = {"id": 1, "username": "stage-smoke", "nickname": "stage",
                 "auth_type": "jwt", "org_id": 1, "role": "admin", "is_admin": True}
    for name in ("get_current_user", "get_current_user_or_api_key", "require_admin",
                 "require_no_session_token", "require_jwt_only", "get_user_or_none"):
        fn = getattr(deps, name, None)
        if fn is not None:
            app.dependency_overrides[fn] = lambda: fake_user
    app.dependency_overrides[deps.require_org_member] = lambda request=None, user=None: fake_user

    paths = READONLY_ENDPOINTS
    if args.all:
        paths = sorted(set(READONLY_ENDPOINTS) | set(discover_get_paths(app)))

    results = []
    # 不进入 lifespan：冒烟只打请求，避免后台线程（日志/补偿循环）在 stage 上起停
    client = TestClient(app)
    if True:
        for path in paths:
            try:
                r = client.get(path)
            except Exception as exc:  # noqa: BLE001 - 记录后端异常而非中断遍历
                results.append({"path": path, "status": 0, "error": str(exc)[:200]})
                continue
            body = r.text or ""
            err = ""
            if r.status_code >= 500 or "doesn't exist" in body or "Unknown column" in body:
                m = re.search(r"(Table '[^']+' doesn't exist|Unknown column '[^']+')", body)
                err = m.group(1) if m else body[:200]
            results.append({"path": path, "status": r.status_code, "error": err})
    client.close()

    bad = [r for r in results if r["status"] >= 500 or r["error"]]
    if args.json:
        print(json.dumps({"total": len(results), "failed": bad}, ensure_ascii=False, indent=2))
    else:
        for r in results:
            mark = "OK " if r["status"] < 500 and not r["error"] else "FAIL"
            extra = f"  {r['error']}" if r["error"] else ""
            print(f"{mark} {r['status']:>3} {r['path']}{extra}")
        print(f"\n{len(results)} 个端点，失败 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())