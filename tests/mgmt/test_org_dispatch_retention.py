"""组织下派/快照 TTL 清理（A7）：终态批次超期清理、非终态保留、磁盘目录同步删除。"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.mgmt import org_retention
from src.kernel.config import WORKSPACE_DIR
from src.kernel.session_manager import db, db_lock

ORG = "issue1820-retention"


def _old() -> str:
    return (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")


def _cleanup() -> None:
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_t_org_dispatch_aggregate WHERE dispatch_id IN "
                  "(SELECT id FROM grape_t_org_dispatch WHERE org_id=?)", (ORG,))
        c.execute("DELETE FROM grape_t_org_dispatch_target WHERE dispatch_id IN "
                  "(SELECT id FROM grape_t_org_dispatch WHERE org_id=?)", (ORG,))
        c.execute("DELETE FROM grape_t_org_context_snapshot WHERE org_id=?", (ORG,))
        c.execute("DELETE FROM grape_t_org_dispatch WHERE org_id=?", (ORG,))
        db.commit()


def test_purges_expired_terminal_and_keeps_inflight():
    _cleanup()
    terminal_id = uuid.uuid4().hex
    inflight_id = uuid.uuid4().hex
    rel_dir = os.path.join("context-snapshots", "org-issue1820-retention",
                           "batch-ret-test")
    abs_dir = os.path.join(WORKSPACE_DIR, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    with open(os.path.join(abs_dir, "manifest.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO grape_t_org_dispatch "
                "(id, org_id, org_issue_number, org_repo_key, scene_id, status, created_at) "
                "VALUES (?,?,?,?,?,'completed',?)",
                (terminal_id, ORG, "1", "gitcode:a/b", "", _old()),
            )
            c.execute(
                "INSERT INTO grape_t_org_dispatch "
                "(id, org_id, org_issue_number, org_repo_key, scene_id, status, created_at) "
                "VALUES (?,?,?,?,?,'running',?)",
                (inflight_id, ORG, "2", "gitcode:a/b", "", _old()),
            )
            c.execute(
                "INSERT INTO grape_t_org_dispatch_target "
                "(id, dispatch_id, repo_key, status) VALUES (?,?,?,'completed')",
                (uuid.uuid4().hex, terminal_id, "gitcode:a/b"),
            )
            c.execute(
                "INSERT INTO grape_t_org_dispatch_aggregate "
                "(id, dispatch_id, terminal_status, status) VALUES (?,?,?,'finalized')",
                (uuid.uuid4().hex, terminal_id, "completed"),
            )
            c.execute(
                "INSERT INTO grape_t_org_context_snapshot "
                "(id, org_id, demand_id, dispatch_id, storage_path, files_json, "
                "content_hash, total_bytes) VALUES (?,?,?,?,?,'{}','',0)",
                (uuid.uuid4().hex, ORG, "1", terminal_id, rel_dir),
            )
            db.commit()

        result = org_retention.purge_expired_org_records(retention_days=90)
        assert result["dispatch_ids"] == 1, result
        assert result["snapshot_dirs"] == 1, result
        assert not os.path.isdir(abs_dir)
        with db_lock:
            c = db.cursor()
            left = [r[0] for r in c.execute(
                "SELECT id FROM grape_t_org_dispatch WHERE org_id=?", (ORG,)).fetchall()]
        assert left == [inflight_id]
    finally:
        _cleanup()
