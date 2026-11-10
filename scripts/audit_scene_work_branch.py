#!/usr/bin/env python3
"""扫描并校正场景高级选项里失效的 work_branch（Issue #1841）。

- 目标：`devops_pipeline_scene.advanced_options.work_branch` 存量数据中，目标仓库
  不存在的分支（如 weekly-1102-zrr）会被原样注入给 Agent，导致按错误基线开发。
- 默认 dry-run：只输出失效清单（原配置值 / 仓库 / 建议回退的默认分支）。
- `--apply`：把失效 work_branch 批量校正为仓库默认分支（保留 custom_fields 等其它字段）。
- 无法拉取分支列表（无 token/网络失败）的记录标记 unknown，不自动改写。
- 幂等可重跑；环境：DEVOPS_DB_PATH 指向待处理库（默认 data/sessions.db）。

用法：
    python scripts/audit_scene_work_branch.py                 # dry-run 全量
    python scripts/audit_scene_work_branch.py --json          # 结构化输出
    python scripts/audit_scene_work_branch.py --repo gitcode:owner/repo
    python scripts/audit_scene_work_branch.py --apply         # 批量校正失效分支
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.session_manager import db, db_lock  # noqa: E402
from src.runtime.repogit import list_repo_branch_names  # noqa: E402


def _scene_rows(repo_key: str = "", limit: int = 0) -> list[dict]:
    sql = ("SELECT scene_id, name, repo_key, advanced_options FROM devops_pipeline_scene "
           "WHERE is_active=1")
    params: list = []
    if repo_key:
        sql += " AND repo_key=?"
        params.append(repo_key)
    sql += " ORDER BY repo_key, scene_id"
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    with db_lock:
        c = db.cursor()
        rows = c.execute(sql, params).fetchall()
    return [{"scene_id": r[0], "name": r[1], "repo_key": r[2],
             "advanced_options": r[3]} for r in rows]


def _suggest_default(branches: set, default_branch: str) -> str:
    if default_branch and default_branch in branches:
        return default_branch
    for cand in ("main", "master"):
        if cand in branches:
            return cand
    return sorted(branches)[0] if branches else (default_branch or "main")


def scan(repo_key: str = "", limit: int = 0) -> dict:
    """扫描存量场景，返回 {invalid, unknown, checked}。

    invalid: 分支不在仓库分支列表 → 可自动校正
    unknown: 分支列表不可核实 → 保持原值
    """
    rows = _scene_rows(repo_key, limit)
    branch_cache: dict = {}
    invalid: list[dict] = []
    unknown: list[dict] = []
    checked = 0
    for row in rows:
        raw = row.get("advanced_options") or ""
        try:
            opts = json.loads(raw) if raw else {}
        except Exception:
            opts = {}
        if not isinstance(opts, dict):
            continue
        wb = (opts.get("work_branch") or "").strip()
        if not wb:
            continue
        checked += 1
        rk = row["repo_key"]
        if rk not in branch_cache:
            branch_cache[rk] = list_repo_branch_names(rk)
        branches, default_branch = branch_cache[rk]
        item = {
            "scene_id": row["scene_id"],
            "name": row.get("name") or "",
            "repo_key": rk,
            "work_branch": wb,
        }
        if branches is None:
            item["status"] = "unknown"
            unknown.append(item)
        elif wb not in branches:
            item["status"] = "invalid"
            item["suggested_branch"] = _suggest_default(branches, default_branch)
            invalid.append(item)
    return {"invalid": invalid, "unknown": unknown, "checked": checked,
            "scenes_total": len(rows)}


def apply_fix(items: list[dict]) -> int:
    """把失效 work_branch 改写为建议默认分支；保留其它高级选项字段。幂等。"""
    applied = 0
    for item in items:
        rk = item["repo_key"]
        scene_id = item["scene_id"]
        new_branch = item.get("suggested_branch") or ""
        if not new_branch:
            continue
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, rk)).fetchone()
            if not row:
                continue
            try:
                opts = json.loads(row[0]) if row[0] else {}
            except Exception:
                opts = {}
            if not isinstance(opts, dict):
                opts = {}
            if (opts.get("work_branch") or "").strip() != item["work_branch"]:
                continue  # 已被其它进程修改，跳过避免覆盖
            opts["work_branch"] = new_branch
            c.execute(
                "UPDATE devops_pipeline_scene SET advanced_options=?, updated_at=? "
                "WHERE scene_id=? AND repo_key=? AND is_active=1",
                (json.dumps(opts, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(),
                 scene_id, rk))
            db.commit()
            applied += 1
    return applied


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Audit and fix stale scene work_branch values (Issue #1841)")
    ap.add_argument("--repo", default="", help="仅处理指定 repo_key（如 gitcode:owner/repo）")
    ap.add_argument("--limit", type=int, default=0, help="最多扫描 N 行（0=不限）")
    ap.add_argument("--apply", action="store_true", help="批量校正失效分支（默认 dry-run）")
    ap.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    args = ap.parse_args(argv)

    result = scan(repo_key=args.repo, limit=args.limit)
    invalid = result["invalid"]
    unknown = result["unknown"]
    applied = 0
    if args.apply and invalid:
        applied = apply_fix(invalid)

    if args.json:
        print(json.dumps({
            "dry_run": not args.apply,
            "checked": result["checked"],
            "scenes_total": result["scenes_total"],
            "invalid": invalid,
            "unknown": unknown,
            "applied": applied,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"[audit] scenes={result['scenes_total']} work_branch_checked={result['checked']} "
              f"invalid={len(invalid)} unknown={len(unknown)}"
              + ("  (dry-run, add --apply to fix)" if not args.apply else ""))
        for it in invalid:
            print("  INVALID  %-16s %-12s %-40s %s -> %s"
                  % (it["repo_key"], it["scene_id"], it["name"][:40],
                     it["work_branch"], it.get("suggested_branch")))
        for it in unknown:
            print("  UNKNOWN  %-16s %-12s %-40s %s (branch list unavailable, kept)"
                  % (it["repo_key"], it["scene_id"], it["name"][:40], it["work_branch"]))
        if args.apply:
            print(f"[audit] applied: {applied} scene(s) corrected to default branch")
    return 0


if __name__ == "__main__":
    sys.exit(main())