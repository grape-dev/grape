#!/usr/bin/env python3
"""存量空 result failed 事件打 legacy 标记（Issue #476 一次性脚本）。

背景：历史失败事件（#476 修复前产生）result 为空/缺失，事件列表出现"无法解释的红"。
原始失败信息已丢失，无法回填真实原因，统一标记 legacy 以区分存量/增量口径：
- result 置 `[legacy] 历史失败事件未记录原因（#476 存量标记）`
- payload 追加 {"_legacy_fail": true, "_legacy_marked_at": "<ts>"}（幂等：已标记跳过）

用法：
    python3 scripts/backfill_legacy_failed_events.py --dry-run       # 只统计不写
    python3 scripts/backfill_legacy_failed_events.py --apply         # 实际标记
    python3 scripts/backfill_legacy_failed_events.py --apply --repo gitcode:owner/repo  # 限定仓库

口径：status='failed' AND (result IS NULL OR TRIM(result)='') AND result NOT LIKE '[legacy]%'
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "sessions.db"))

from src.session_manager import db, db_lock  # noqa: E402

LEGACY_REASON = "[legacy] 历史失败事件未记录原因（#476 存量标记）"


def _candidate_rows(repo=""):
    sql = ("SELECT seq, payload FROM events WHERE status='failed' "
           "AND (result IS NULL OR TRIM(result)='') AND result NOT LIKE '[legacy]%'")
    params: list = []
    if repo:
        sql += " AND repo_key=?"
        params.append(repo)
    with db_lock:
        c = db.cursor()
        c.execute(sql + " ORDER BY seq", params)
        return c.fetchall()


def main():
    ap = argparse.ArgumentParser(description="标记存量空 result failed 事件为 legacy")
    ap.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run）")
    ap.add_argument("--repo", default="", help="限定仓库 repo_key（如 gitcode:owner/repo）")
    args = ap.parse_args()

    rows = _candidate_rows(args.repo)
    print(f"[#476] 候选空 result failed 事件：{len(rows)} 条"
          + (f"（repo={args.repo}）" if args.repo else "（全部仓库）"))
    if not args.apply:
        print("[#476] dry-run 模式，未写入。加 --apply 实际标记。")
        return 0

    ts = datetime.now(timezone.utc).isoformat()
    marked = 0
    with db_lock:
        c = db.cursor()
        for seq, payload_raw in rows:
            payload = {}
            try:
                payload = json.loads(payload_raw or "{}") if payload_raw else {}
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            if payload.get("_legacy_fail"):
                continue  # 幂等：已标记跳过
            payload["_legacy_fail"] = True
            payload["_legacy_marked_at"] = ts
            c.execute(
                "UPDATE events SET result=?, payload=?, exit_code=COALESCE(exit_code, 1), "
                "completed_at=COALESCE(NULLIF(completed_at,''), ?) WHERE seq=?",
                (LEGACY_REASON, json.dumps(payload, ensure_ascii=False), ts, seq))
            marked += 1
        db.commit()
    print(f"[#476] 已标记 {marked} 条 legacy（原因：{LEGACY_REASON}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())