#!/usr/bin/env python3
"""补绑无场景 Issue 的仓库默认场景（Issue #415）。

- 目标：scene_id 为空/未设置的存量 Issue（如 #406 / 需求 #3436）按仓库默认场景
  （managed_repos.dev_scene）补绑，使需求详情页「流水线场景」不再显示 --。
- 默认只打印候选（dry-run），`--apply` 才写库；幂等可重跑。
- 仓库未配置默认场景（dev_scene 为空）的 Issue 无法补绑，dry-run 中标注
  skip(no repo default)，保持展示侧回退语义（--）。
- 环境：DEVOPS_DB_PATH 指向待处理库（默认 data/sessions.db）。

用法：
    python scripts/backfill_issue_scene.py            # dry-run：列出候选行
    python scripts/backfill_issue_scene.py --apply    # 执行补绑
    python scripts/backfill_issue_scene.py --json     # 结构化输出候选
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.session_manager import db, db_lock  # noqa: E402


def candidates() -> list[dict]:
    """返回待补绑候选行（scene_id 为空 + 解析仓库默认场景）。"""
    from src.user.db import get_repo_scene
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, repo_key, issue_number, title FROM issue "
            "WHERE (scene_id IS NULL OR scene_id = '') "
            "ORDER BY id"
        )
        raw = c.fetchall()
    rows = []
    for _id, _rk, _num, _title in raw:
        _target = ""
        try:
            _target = get_repo_scene(_rk) or ""
        except Exception:
            _target = ""
        rows.append({
            "id": _id, "repo_key": _rk, "issue_number": _num,
            "title": (_title or "")[:60], "target_scene": _target,
        })
    return rows


def apply() -> int:
    """为可补绑候选写入仓库默认场景，返回受影响行数；幂等。"""
    from src.api.session_store import set_issue_scene
    affected = 0
    for row in candidates():
        if not row["target_scene"]:
            continue
        try:
            set_issue_scene(row["repo_key"], f"#{row['issue_number']}", row["target_scene"])
            affected += 1
        except Exception as e:
            print(f"  [warn] id={row['id']} {row['repo_key']} #{row['issue_number']} bind failed: {e}")
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill issue.scene_id = repo default scene (Issue #415)")
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
            _status = "bind" if r["target_scene"] else "skip(no repo default)"
            print("  id=%s  %s #%s  %-56s -> %s  [%s]"
                  % (r["id"], r["repo_key"], r["issue_number"], r["title"],
                     r["target_scene"] or "--", _status))
    if not args.apply:
        return 0
    n = apply()
    print(f"[backfill] applied: {n} rows updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())