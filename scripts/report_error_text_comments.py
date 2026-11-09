#!/usr/bin/env python3
"""Issue #1545 E1：只读扫描历史「可能已外泄」的报错文本回帖清单。

用途：找出 agent_tasks.result_text / events.result 中命中 `_looks_like_error_text`
的历史产出，输出 repo_key、issue_number、seq 与摘要，供人工判断是否需要后续处理。

约束：本脚本只执行 SELECT，不调用任何删除/编辑接口，也不修改数据库
（GitCode 历史评论不可删，故只出清单）。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.api.event_pipeline import _looks_like_error_text  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402


def _scan_agent_tasks(limit):
    rows = []
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, event_seq, issue_id, repo_key, agent_type, result_text "
            "FROM agent_tasks WHERE result_text IS NOT NULL AND result_text != '' "
            "ORDER BY created_at DESC LIMIT ?", (limit,))
        for tid, seq, issue_id, repo_key, agent_type, result_text in c.fetchall():
            if _looks_like_error_text(result_text):
                rows.append({
                    "source": "agent_tasks",
                    "task_id": tid,
                    "seq": seq,
                    "issue_number": str(issue_id or "").lstrip("#"),
                    "repo_key": repo_key or "",
                    "agent_type": agent_type or "",
                    "summary": (result_text or "").strip().replace("\n", " ")[:200],
                })
    return rows


def _scan_events(limit):
    rows = []
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT seq, issue_id, repo_key, agent_type, result "
            "FROM events WHERE result IS NOT NULL AND result != '' "
            "ORDER BY seq DESC LIMIT ?", (limit,))
        for seq, issue_id, repo_key, agent_type, result in c.fetchall():
            if _looks_like_error_text(result):
                rows.append({
                    "source": "events",
                    "task_id": "",
                    "seq": seq,
                    "issue_number": str(issue_id or "").lstrip("#"),
                    "repo_key": repo_key or "",
                    "agent_type": agent_type or "",
                    "summary": (result or "").strip().replace("\n", " ")[:200],
                })
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Issue #1545 只读清单：可能已外泄的引擎报错文本（不修改任何数据）")
    parser.add_argument("--limit", type=int, default=1000,
                        help="每张表最多扫描行数（默认 1000）")
    parser.add_argument("--json", action="store_true", help="以 JSON 数组输出")
    args = parser.parse_args()

    rows = _scan_agent_tasks(args.limit) + _scan_events(args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(f"# 可能已外泄清单：{len(rows)} 条（只读扫描，未修改任何数据）")
        for r in rows:
            print(f"- [{r['source']}] repo={r['repo_key']} issue=#{r['issue_number']} "
                  f"seq={r['seq']} agent={r['agent_type']} :: {r['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())