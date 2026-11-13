#!/usr/bin/env python3
"""P8-consistency 存量回填：issue 型 autopilot 失败任务补 issue_id 回链键。

背景：agent_tasks 中 event_type='autopilot_event' 且 agent_type='issue' 的行
（旧代码创建入队时 issue_id 恒空——成功/失败行都空）整体丢出 dashboard issue 统计
（dashboard.py:565 按 issue_id IS NOT NULL 分组）。修复后新任务创建即落
无数字合成键 autopilot-<letters>；本脚本把存量空回链行回填为同一确定性键
（同一 autopilot 任务历史行与未来行分组一致）。

关联规则（关联不上不硬造）：
- agent_tasks.id -> autopilot_executions.agent_task_id -> autopilot_executions.task_id
  -> autopilot_tasks.id -> _autopilot_issue_ref(task_id)；
- agent_task_id 为空的历史行走兜底 event_seq=autopilot_executions.id；
- 两条都关联不上（无 autopilot 任务行）→ 跳过并报告，不写值；
- vod 型空回链是设计口径（不属 issue 统计），本脚本不触碰。

用法：
  python3 scripts/backfill_p8_autopilot_issue_ref.py            # dry-run（默认）
  python3 scripts/backfill_p8_autopilot_issue_ref.py --apply    # 实际回填
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)


def _issue_ref(task_id) -> str:
    # 与 src/domains/runtime/engine/scheduler.py::_autopilot_issue_ref 同口径
    # （此处独立实现，避免拉起整个 app 依赖；两处同步由回归用例锁定）
    try:
        n = int(task_id or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("a") + r) + s
    return f"autopilot-{s}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    args = ap.parse_args()
    db_path = os.environ.get("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "sessions.db"))
    c = sqlite3.connect(db_path)

    rows = c.execute(
        "SELECT id, event_seq, repo_key, status FROM agent_tasks "
        "WHERE (issue_id IS NULL OR issue_id='') "
        "AND event_type='autopilot_event' AND agent_type='issue'").fetchall()
    print(f"候选空回链 issue 型任务: {len(rows)}")

    # exec 映射：agent_task_id -> task_id；event_seq(=exec id) -> task_id
    at_map, seq_map = {}, {}
    for exec_id, task_id, agent_task_id in c.execute(
            "SELECT id, task_id, agent_task_id FROM autopilot_executions"):
        if agent_task_id:
            at_map[str(agent_task_id)] = task_id
        seq_map[int(exec_id)] = task_id

    updates, skipped = [], []
    for tid, seq, repo_key, status in rows:
        src = ""
        ap_task_id = at_map.get(str(tid)) or (seq_map.get(int(seq or 0)) if seq else None)
        if ap_task_id:
            ref = _issue_ref(ap_task_id)
            if ref:
                updates.append((ref, tid))
                src = f"autopilot_tasks.id={ap_task_id}"
            else:
                skipped.append((tid, f"autopilot_tasks.id={ap_task_id} 编码失败"))
        else:
            skipped.append((tid, "关联不上 autopilot 任务行（agent_task_id/event_seq 均无映射）"))
        print(f"  {tid} seq={seq} status={status} repo={repo_key} -> "
              f"{src or 'SKIP'}")

    for tid, why in skipped:
        print(f"  SKIP {tid}: {why}（不硬造，保持空）")

    if not args.apply:
        print(f"\ndry-run: 可回填 {len(updates)} 条，跳过 {len(skipped)} 条。加 --apply 执行。")
        return
    if updates:
        with c:
            c.executemany(
                "UPDATE agent_tasks SET issue_id=? WHERE id=? AND (issue_id IS NULL OR issue_id='')",
                updates)
    print(f"\n回填完成: {len(updates)} 条更新, {len(skipped)} 条跳过。")


if __name__ == "__main__":
    main()
