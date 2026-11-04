#!/usr/bin/env python3
"""回填 issue.created_by 空值的存量行（Issue #805：创建人一等数据化）。

- 目标：`issue.created_by` 为空的行 → 经 VCS issue 详情（fetch_issue_via_vcs）读取
  GitCode/GitHub issue 作者 login 回填。
- 默认 dry-run（只打印计划，不写库）；`--apply` 才写库；幂等仅空值补写（不覆盖已有创建人）。
- 限流：每条 VCS 调用后 sleep（默认 0.5s，`--sleep` 可调），防止 API 限流。
- `--limit`：最多处理 N 条（默认不限）；`--repo`：仅处理指定 repo_key。

用法：
    python scripts/backfill_issue_creator.py                 # dry-run 全量
    python scripts/backfill_issue_creator.py --limit 50      # dry-run 前 50 条
    python scripts/backfill_issue_creator.py --apply         # 实跑回填
    python scripts/backfill_issue_creator.py --apply --repo gitcode:owner/repo --sleep 1

验证：apply 后 `created_by=''` 计数应为 0（VCS 无法核实的行会列入 unresolved 清单）。
环境：DEVOPS_DB_PATH 指向待处理库（默认 data/sessions.db）。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.session_manager import db, db_lock  # noqa: E402


def _empty_created_by_rows(repo_key: str = "", limit: int = 0) -> list[dict]:
    """返回 created_by 为空的候选行（按 repo 分组顺序：repo_key, issue_number）。"""
    sql = ("SELECT id, repo_key, issue_number FROM issue "
           "WHERE (created_by IS NULL OR created_by='') AND issue_number > 0 "
           "ORDER BY repo_key, issue_number")
    params: list = []
    if repo_key:
        sql = sql.replace("ORDER BY", "AND repo_key=? ORDER BY")
        params.append(repo_key)
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    with db_lock:
        c = db.cursor()
        rows = c.execute(sql, params).fetchall()
    return [{"id": r[0], "repo_key": r[1], "issue_number": r[2]} for r in rows]


def _resolve_creator(repo_key: str, issue_number: int) -> str:
    """经 VCS issue 详情读创建人 login；无法核实返回空串。"""
    from src.api.event_pipeline import fetch_issue_via_vcs
    try:
        state, data = fetch_issue_via_vcs(repo_key, issue_number)
    except Exception:
        return ""
    if state != "exists":
        return ""
    user = (data or {}).get("user") or {}
    if not isinstance(user, dict):
        return ""
    return (user.get("login") or user.get("username") or "").strip()


def _update_created_by(row_id: int, creator: str) -> int:
    """幂等回填：仅 created_by 为空时写入，返回受影响行数。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET created_by=? WHERE id=? AND (created_by IS NULL OR created_by='')",
            (creator, row_id))
        db.commit()
        return c.rowcount if c.rowcount and c.rowcount > 0 else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill issue.created_by from VCS issue author (Issue #805)")
    ap.add_argument("--apply", action="store_true", help="实跑写库（默认 dry-run 只打印计划）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0=不限）")
    ap.add_argument("--repo", default="", help="仅处理指定 repo_key（如 gitcode:owner/repo）")
    ap.add_argument("--sleep", type=float, default=0.5, help="每条 VCS 调用后的限流间隔秒数")
    args = ap.parse_args()

    rows = _empty_created_by_rows(args.repo, args.limit)
    total = len(rows)
    print(f"[backfill_issue_creator] mode={'APPLY' if args.apply else 'DRY-RUN'} "
          f"candidates={total} repo={args.repo or '(all)'} limit={args.limit or '(none)'}")

    backfilled = 0
    unresolved: list[dict] = []
    for i, row in enumerate(rows, 1):
        creator = _resolve_creator(row["repo_key"], row["issue_number"])
        if creator:
            print(f"[{i}/{total}] #{row['issue_number']} {row['repo_key']} -> creator={creator}"
                  + ("" if args.apply else "  (dry-run, not written)"))
            if args.apply:
                backfilled += _update_created_by(row["id"], creator)
        else:
            unresolved.append(row)
            print(f"[{i}/{total}] #{row['issue_number']} {row['repo_key']} -> UNRESOLVED (VCS 无法核实作者)")
        if args.sleep > 0:
            time.sleep(args.sleep)

    if unresolved:
        print(f"[backfill_issue_creator] unresolved={len(unresolved)}（VCS 无法核实，需人工核对清单）:")
        for row in unresolved:
            print(f"  - id={row['id']} {row['repo_key']} #{row['issue_number']}")
    remaining = len(_empty_created_by_rows(args.repo, 0))
    print(f"[backfill_issue_creator] done: backfilled={backfilled} unresolved={len(unresolved)} "
          f"remaining_empty={remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
