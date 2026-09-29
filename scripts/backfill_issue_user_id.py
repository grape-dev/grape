#!/usr/bin/env python3
"""回填 issue.user_id=0 且 assignee 可映射的存量行（issues-mine-user-id-zero-fix-design.md §3.3）。

- 目标：assignee 在 unified_user_mapping 可映射到已注册用户、但 issue.user_id=0 的行 → 回填真实 user_id。
- 默认只打印候选（dry-run），`--apply` 才写库；幂等可重跑。
- 环境：DEVOPS_DB_PATH 指向待处理库（默认 data/sessions.db）。
- 验证：apply 后 `user_id=0 且 assignee 可映射` 计数应为 0。

用法：
    python scripts/backfill_issue_user_id.py            # dry-run：列出候选行
    python scripts/backfill_issue_user_id.py --apply    # 执行回填
    python scripts/backfill_issue_user_id.py --json     # 结构化输出候选（id/repo/number/assignee/待填 user_id）
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.session_manager import db, db_lock  # noqa: E402

_SQL_CANDIDATES = """
SELECT i.id, i.repo_key, i.issue_number, i.assignee,
       (SELECT m.user_id FROM unified_user_mapping m
        WHERE m.gitcode_user = i.assignee AND m.user_id > 0 LIMIT 1) AS mapped_user_id
FROM issue i
WHERE i.user_id = 0
  AND i.assignee != ''
  AND EXISTS (SELECT 1 FROM unified_user_mapping m
              WHERE m.gitcode_user = i.assignee AND m.user_id > 0)
ORDER BY i.id
"""


def candidates() -> list[dict]:
    """返回待回填候选行。"""
    with db_lock:
        c = db.cursor()
        rows = c.execute(_SQL_CANDIDATES).fetchall()
    return [
        {"id": r[0], "repo_key": r[1], "issue_number": r[2],
         "assignee": r[3], "mapped_user_id": r[4]}
        for r in rows
    ]


def apply() -> int:
    """回填 user_id（0 → 映射 id），返回受影响行数；幂等。"""
    affected = 0
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue "
            "SET user_id = (SELECT m.user_id FROM unified_user_mapping m "
            "               WHERE m.gitcode_user = issue.assignee AND m.user_id > 0 LIMIT 1) "
            "WHERE issue.user_id = 0 "
            "  AND issue.assignee != '' "
            "  AND EXISTS (SELECT 1 FROM unified_user_mapping m "
            "              WHERE m.gitcode_user = issue.assignee AND m.user_id > 0)",
        )
        affected = c.rowcount
        db.commit()
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill issue.user_id=0 (assignee mappable) rows")
    parser.add_argument("--apply", action="store_true", help="write back; default is dry-run")
    parser.add_argument("--json", action="store_true", help="output candidate list as JSON")
    args = parser.parse_args()

    rows = candidates()
    if args.json:
        print(json.dumps({"dry_run": not args.apply, "rows": rows,
                          "count": len(rows)}, ensure_ascii=False, indent=2))
    else:
        print(f"[backfill] candidates: {len(rows)}"
              + ("  (dry-run, add --apply to write)" if not args.apply else ""))
        for r in rows:
            print("  id=%s  %s #%s  assignee=%s -> user_id=%s"
                  % (r["id"], r["repo_key"], r["issue_number"], r["assignee"], r["mapped_user_id"]))
    if not args.apply:
        return 0
    n = apply()
    print(f"[backfill] applied: {n} rows updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())