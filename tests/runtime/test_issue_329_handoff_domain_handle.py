# -*- coding: utf-8 -*-
"""Cross-pipeline handoff must not depend on the process-own database handle.

Background: `issue` / `events` / `agent_tasks` are owned by the runtime domain
(`configs/table_ownership.json`). The cross-pipeline handoff is served in-process
by the mgmt process (`POST /api/issues/{id}/handoff` -> `_dispatch_a2a` ->
`_cross_pipeline_handoff`), where `session_manager.db` points at the mgmt domain
DB. A raw read there raises "table does not exist"; historically the audit read
shared one `try` with the state writes, so a single failing SELECT dropped
`scene_id` / `stage` / `carried_context` while the endpoint still answered 200.

This module locks two properties:
1. Static: `webhook.py` holds no bare process-own handle on runtime tables.
2. Behaviour: a failing audit read never blocks scene / stage / carried_context.
"""

import ast
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_issue_329_handoff_domain.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME", "DEVOPS_DB_PORT"):
    os.environ.pop(_k, None)

import pytest

from src.api.db_setup import init_database
from src.kernel.session_manager import db, db_lock
from src.domains.runtime.routes.webhook import _cross_pipeline_handoff

init_database()

REPO = "gitcode:org/test-329-handoff"

WEBHOOK_PATH = os.path.join(ROOT, "src", "domains", "runtime", "routes", "webhook.py")


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with db_lock:
            c = db.cursor()
            for _sql, _args in (
                ("DELETE FROM issue WHERE repo_key=?", (REPO,)),
                ("DELETE FROM events WHERE repo_key=?", (REPO,)),
                ("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,)),
                ("DELETE FROM event_active_guard WHERE repo_key=?", (REPO,)),
            ):
                c.execute(_sql, _args)
            db.commit()

    _wipe()
    yield
    _wipe()


def _set_issue(number, scene_id="", assignee=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, scene_id) "
            "VALUES (1, ?, ?, ?, 'pending_review', ?)",
            (assignee, REPO, number, scene_id),
        )
        db.commit()


def _set_scene(scene_id, nodes):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, display_name, repo_key, is_active, nodes_json) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (scene_id, scene_id, scene_id, REPO, json.dumps(nodes)),
        )
        db.commit()


# === static guards ===


def _session_manager_db_aliases(path):
    """Collect local names bound to `session_manager.db` in one module."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src.kernel.session_manager"):
            for alias in node.names:
                if alias.name == "db":
                    names.append(alias.asname or alias.name)
    return names


def test_no_process_own_handle_for_runtime_tables():
    """webhook.py must not bind the process-own DB (issue/events live in runtime)."""
    aliases = _session_manager_db_aliases(WEBHOOK_PATH)
    assert aliases == [], f"process-own DB still imported for runtime tables: {aliases}"

    src = open(WEBHOOK_PATH, encoding="utf-8").read()
    assert "db.cursor()" not in src, "bare process-own cursor() left on a runtime table read"


def test_runtime_reads_use_domain_handle():
    """Runtime reads go through the runtime handle helper, writes through public primitives."""
    src = open(WEBHOOK_PATH, encoding="utf-8").read()
    assert "_runtime_db()" in src
    # carried_context is a runtime-domain write: it must use the public primitive,
    # not a raw UPDATE issued on the process-own connection.
    assert "_sync_issue_fields" in src
    assert "UPDATE issue SET carried_context" not in src


# === behaviour guard ===


def test_audit_read_failure_does_not_block_state_writes(monkeypatch):
    """A failing assignee audit read must not drop scene/stage/carried_context."""
    import src.api.session_store as session_store

    def _boom(*_a, **_kw):
        raise RuntimeError("table does not exist")

    monkeypatch.setattr(session_store, "get_issue_assignee", _boom)

    _set_issue(104, "source-scene")
    _set_scene("target-scene", [{"id": "vod"}])
    carried = {"source_issue_number": 104, "spec_ref": "spec-v4"}

    result = _cross_pipeline_handoff(
        REPO, "104", "target-scene", "vod",
        carried, "", None, "test", "org/test-329", "org/test-329",
    )
    assert result["ok"] is True

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT scene_id, stage, carried_context FROM issue "
            "WHERE repo_key=? AND issue_number=104",
            (REPO,),
        )
        row = c.fetchone()
    assert row is not None
    assert row[0] == "target-scene", "scene_id was not switched to the target scene"
    assert row[1] == "vod", "stage was not advanced to the target node"
    persisted = json.loads(row[2] or "{}")
    assert persisted.get("spec_ref") == "spec-v4"