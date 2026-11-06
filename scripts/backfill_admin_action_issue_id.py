#!/usr/bin/env python3
"""Issue #1312: backfill admin_action audit events with real business ownership.

Default is dry-run. Use --apply to write, --normalize-unrelated to rewrite
issue_id='#0' rows to '', and --rollback <backup> to restore a normalize run.

Usage:
    python3 scripts/backfill_admin_action_issue_id.py
    python3 scripts/backfill_admin_action_issue_id.py --apply
    python3 scripts/backfill_admin_action_issue_id.py --normalize-unrelated --apply
    python3 scripts/backfill_admin_action_issue_id.py --rollback <backup.json>
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_BATCH_SIZE = 500


def _load_payload(raw):
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _count_ghost_admin(c):
    c.execute(
        "SELECT COUNT(*) FROM events e WHERE e.event_type='admin_action' "
        "AND e.issue_id NOT IN ('', '#0') AND NOT EXISTS ("
        "SELECT 1 FROM issue i WHERE i.repo_key=e.repo_key "
        "AND i.issue_number=CAST(SUBSTR(e.issue_id, 2) AS INTEGER))")
    return c.fetchone()[0] or 0


def _count_unrelated(c):
    c.execute("SELECT COUNT(*) FROM events WHERE issue_id='#0'")
    return c.fetchone()[0] or 0


def _plan_backfill(c):
    c.execute("SELECT seq, repo_key, issue_id, payload FROM events WHERE event_type='admin_action'")
    rows = c.fetchall()
    changes = []
    for seq, repo_key, issue_id, raw in rows:
        payload = _load_payload(raw)
        target_seq = payload.get("target_event_seq")
        if not target_seq:
            continue
        c.execute("SELECT repo_key, issue_id FROM events WHERE seq=?", (target_seq,))
        target = c.fetchone()
        if not target:
            continue
        new_repo = target[0] or repo_key or ""
        new_issue = target[1] or ""
        if new_issue == "#0":
            new_issue = ""
        if new_repo == (repo_key or "") and new_issue == (issue_id or ""):
            continue
        changes.append({
            "seq": seq, "old_repo_key": repo_key or "", "old_issue_id": issue_id or "",
            "new_repo_key": new_repo, "new_issue_id": new_issue,
            "target_event_seq": target_seq,
        })
    return changes


def _apply_changes(c, conn, changes):
    done = 0
    for start in range(0, len(changes), _BATCH_SIZE):
        batch = changes[start:start + _BATCH_SIZE]
        for item in batch:
            c.execute("UPDATE events SET repo_key=?, issue_id=? WHERE seq=?",
                      (item["new_repo_key"], item["new_issue_id"], item["seq"]))
            done += c.rowcount
        conn.commit()
    return done


def _backup_path():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", f"backfill_admin_action_issue_id_{stamp}.json")


def _write_backup(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return path


def _normalize_unrelated(c, conn, backup_file):
    c.execute("SELECT seq, issue_id FROM events WHERE issue_id='#0' ORDER BY seq")
    rows = [{"seq": r[0], "old_issue_id": r[1]} for r in c.fetchall()]
    path = _write_backup(rows, backup_file)
    done = 0
    for start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[start:start + _BATCH_SIZE]
        for item in batch:
            c.execute("UPDATE events SET issue_id='' WHERE seq=? AND issue_id='#0'", (item["seq"],))
            done += c.rowcount
        conn.commit()
    return len(rows), done, path


def _rollback(c, conn, backup_file):
    with open(backup_file, encoding="utf-8") as f:
        rows = json.load(f)
    done = 0
    for start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[start:start + _BATCH_SIZE]
        for item in batch:
            c.execute("UPDATE events SET issue_id=? WHERE seq=?",
                      (item.get("old_issue_id", ""), item["seq"]))
            done += c.rowcount
        conn.commit()
    return done


def main():
    parser = argparse.ArgumentParser(description="Backfill admin_action issue ownership (Issue #1312)")
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    parser.add_argument("--normalize-unrelated", action="store_true",
                        help="rewrite issue_id='#0' rows to '' (backs up seq/old_issue_id first)")
    parser.add_argument("--rollback", default="", metavar="BACKUP_FILE",
                        help="restore issue_id from a normalize backup file")
    parser.add_argument("--backup-file", default="", help="normalize backup path")
    args = parser.parse_args()

    from src.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        if args.rollback:
            if not os.path.exists(args.rollback):
                print(f"[backfill] rollback file not found: {args.rollback}", file=sys.stderr)
                return 2
            n = _rollback(c, db, args.rollback)
            print(f"[backfill] rollback restored: {n} rows")
            return 0

        if args.normalize_unrelated:
            before = _count_unrelated(c)
            if not args.apply:
                c.execute("SELECT seq, issue_id FROM events WHERE issue_id='#0' ORDER BY seq LIMIT 10")
                samples = [{"seq": r[0], "old_issue_id": r[1]} for r in c.fetchall()]
                print(f"[backfill] normalize candidates: {before} (dry-run, add --apply to write)")
                for s in samples:
                    print(f"  seq={s['seq']} issue_id={s['old_issue_id']!r} -> ''")
                return 0
            backup = args.backup_file or _backup_path()
            total, done, path = _normalize_unrelated(c, db, backup)
            after = _count_unrelated(c)
            print(f"[backfill] normalize before={before} selected={total} updated={done} "
                  f"after={after} backup={path}")
            return 0

        before_ghost = _count_ghost_admin(c)
        changes = _plan_backfill(c)
        if not args.apply:
            print(f"[backfill] admin_action candidates: {len(changes)} "
                  f"(dry-run, add --apply to write); ghost_before={before_ghost}")
            for item in changes[:10]:
                print("  seq=%s %s %s -> %s %s (target=%s)" % (
                    item["seq"], item["old_repo_key"], item["old_issue_id"],
                    item["new_repo_key"], item["new_issue_id"], item["target_event_seq"]))
            return 0
        done = _apply_changes(c, db, changes)
        after_ghost = _count_ghost_admin(c)
        print(f"[backfill] admin_action before={before_ghost} selected={len(changes)} "
              f"updated={done} after={after_ghost}")
    return 0


if __name__ == "__main__":
    sys.exit(main())