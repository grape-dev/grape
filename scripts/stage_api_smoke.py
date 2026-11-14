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


# 路径参数取值来源（只读）：param -> (domain, SQL 取单值)
PARAM_SOURCES: dict[str, tuple[str, str]] = {
    "agent_type": ("runtime", "SELECT agent_type FROM agent_type ORDER BY order_num LIMIT 1"),
    "cid": ("runtime", "SELECT id FROM runtime_credentials ORDER BY id LIMIT 1"),
    "conv_id": ("runtime", "SELECT id FROM chat_conversations ORDER BY updated_at DESC LIMIT 1"),
    "event_seq": ("runtime", "SELECT seq FROM events ORDER BY seq DESC LIMIT 1"),
    "seq": ("runtime", "SELECT seq FROM events ORDER BY seq DESC LIMIT 1"),
    "exec_id": ("runtime", "SELECT id FROM autopilot_executions ORDER BY id DESC LIMIT 1"),
    "knowledge_id": ("mgmt", "SELECT id FROM agent_shared_knowledge ORDER BY id LIMIT 1"),
    "market_id": ("mgmt", "SELECT id FROM scene_market ORDER BY id LIMIT 1"),
    "number": ("runtime", "SELECT issue_number FROM issue ORDER BY issue_number LIMIT 1"),
    "issue_id": ("runtime", "SELECT issue_id FROM events WHERE issue_id<>'' ORDER BY seq DESC LIMIT 1"),
    "org_id": ("mgmt", "SELECT id FROM organization ORDER BY id LIMIT 1"),
    "owner": ("mgmt", "SELECT repo_owner FROM managed_repos ORDER BY id LIMIT 1"),
    "repo": ("mgmt", "SELECT repo_name FROM managed_repos ORDER BY id LIMIT 1"),
    "repo_name": ("mgmt", "SELECT repo_name FROM managed_repos ORDER BY id LIMIT 1"),
    "rid": ("runtime", "SELECT id FROM agent_runtimes ORDER BY id LIMIT 1"),
    "runtime_id": ("runtime", "SELECT id FROM agent_runtimes ORDER BY id LIMIT 1"),
    "scene_id": ("mgmt", "SELECT id FROM devops_pipeline_scene ORDER BY id LIMIT 1"),
    "session_id": ("runtime", "SELECT id FROM devops_agent_session ORDER BY id LIMIT 1"),
    "skill_id": ("runtime", "SELECT id FROM skill ORDER BY id LIMIT 1"),
    "task_id": ("mgmt", "SELECT id FROM autopilot_tasks ORDER BY id LIMIT 1"),
    "user_id": ("mgmt", "SELECT id FROM grape_accounts ORDER BY id LIMIT 1"),
}
FIXED_PARAMS = {"provider": "gitcode"}


def resolve_params(app, with_ids: bool) -> tuple[list[str], list[str]]:
    """把带参数路径替换为真实值；无法解析的参数返回 skip 列表。"""
    from src.api.openapi import _iter_routes

    if not with_ids:
        return sorted({getattr(r, "path", "") for route in app.routes
                       for r in _iter_routes(route)
                       if "GET" in (getattr(r, "methods", set()) or set())
                       and getattr(r, "path", "").startswith("/api/") and "{" not in getattr(r, "path", "")}), []
    from src.kernel.domain_db import cursor as _domain_cursor

    values: dict[str, str] = dict(FIXED_PARAMS)
    for name, (domain, sql) in PARAM_SOURCES.items():
        if name in values:
            continue
        try:
            with _domain_cursor(domain) as c:
                c.execute(sql)
                row = c.fetchone()
            if row and row[0] not in (None, ""):
                values[name] = str(row[0])
        except Exception:  # noqa: BLE001 - 取不到就跳过该参数
            continue

    ok, skipped = [], []
    for route in app.routes:
        for r in _iter_routes(route):
            path = getattr(r, "path", "")
            if "GET" not in (getattr(r, "methods", set()) or set()) or not path.startswith("/api/"):
                continue
            names = re.findall(r"\{(\w+)\}", path)
            if not names:
                ok.append(path)
                continue
            if any(n not in values for n in names):
                skipped.append(path + "  (" + ",".join(n for n in names if n not in values) + ")")
                continue
            filled = path
            for n in names:
                filled = filled.replace("{" + n + "}", values[n])
            ok.append(filled)
    return sorted(set(ok)), sorted(set(skipped))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="stage API smoke（视图清零环境）")
    ap.add_argument("--all", action="store_true", help="自动发现所有无路径参数的 /api/* GET")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--with-ids", action="store_true",
                    help="路径参数用 stage 库中的真实 id 填充（只读详情页覆盖）")
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

    skipped: list[str] = []
    if args.all or args.with_ids:
        paths, skipped = resolve_params(app, with_ids=args.with_ids)
        paths = sorted(set(paths) | set(READONLY_ENDPOINTS))
    else:
        paths = READONLY_ENDPOINTS

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
            pat = r"(Table '[^']+' doesn't exist|Unknown column '[^']+')"
            if r.status_code >= 500:
                m = re.search(pat, body)
                err = m.group(1) if m else body[:200]
            else:
                # 只认结构化错误字段（detail/message/error），避免把"日志列表里的一条历史
                # 错误文本"误判成该端点失败（/api/dashboard/ops/error-logs 就是日志本身）。
                try:
                    payload = r.json()
                except Exception:  # noqa: BLE001
                    payload = None
                if isinstance(payload, dict):
                    for key in ("detail", "message", "error", "detail_message"):
                        val = payload.get(key)
                        text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
                        m = re.search(pat, text or "")
                        if m:
                            err = m.group(1)
                            break
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
        if skipped:
            print(f"跳过 {len(skipped)} 个（参数无法从 stage 数据解析）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())