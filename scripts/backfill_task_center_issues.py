#!/usr/bin/env python3
"""存量回填（task-center-issue-coverage-design.md P6）：让任务中心覆盖全部存量 Issue。

幂等：ensure_issue_task 对同 repo+issue+assignee 的 active/processing 行复用；
已有任意行（含 completed）的 issue 直接跳过，避免给已收尾的 Issue 重开 active 任务。

用法：
  .venv/bin/python scripts/backfill_task_center_issues.py
  # 仅统计不加数据：--dry-run
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> list:  # type: ignore[return]
    dry_run = "--dry-run" in sys.argv
    os.environ.setdefault("DEVOPS_DB_BACKEND", "mysql")

    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    from src.api.routes.tasks import ensure_issue_task

    with db_lock:
        c = db.cursor()
        c.execute("SELECT DISTINCT repo_key, issue_id FROM issue_state WHERE issue_id IS NOT NULL AND issue_id != ''")
        items = [(r[0], r[1]) for r in c.fetchall()]

    created = skipped = 0
    issues = []
    for repo_key_raw, iid in items:
        num = str(iid).lstrip("#")
        if not num.isdigit() or int(num) <= 0:
            continue
        if str(repo_key_raw).count(":") > 1:  # 脏行（已有双前缀），跳过
            skipped += 1
            continue
        repo_key = to_repo_key(repo_key_raw)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM tasks WHERE repo_key=? AND issue_number=?", (repo_key, int(num)))
            has_row = c.fetchone()
        if has_row:
            skipped += 1
            continue
        if dry_run:
            created += 1
            issues.append((repo_key, int(num)))
            continue
        ensure_issue_task(repo_key, int(num))
        created += 1
        issues.append((repo_key, int(num)))

    print(f"{'DRY-RUN ' if dry_run else ''}回填完成: 新建 {created} 条, 跳过(已有行) {skipped} 条")
    if dry_run or created <= 20:
        for rk, n in issues:
            print(f"  {rk} #{n}")

    # 第二阶段：存量任务补「去对话」会话关联（user_id>0 且未关联）
    from src.api.routes.tasks import _ensure_task_conversation
    linked = 0
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, user_id, repo_key, issue_number, title, task_description FROM tasks WHERE user_id>0 AND (conversation_id IS NULL OR conversation_id='')")
        rows = c.fetchall()
    for r in rows:
        conv_id = _ensure_task_conversation(r[1], r[2], r[3], r[4], r[5])
        if conv_id:
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE tasks SET conversation_id=? WHERE id=?", (conv_id, r[0]))
                db.commit()
            linked += 1
    print(f"补对话关联: {linked} 条 (共 {len(rows)} 条缺关联)")
    return issues


if __name__ == "__main__":
    main()