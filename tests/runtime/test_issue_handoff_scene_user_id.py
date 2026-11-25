"""Cross-pipeline handoff must not clobber issue.scene_user_id with 0.

Failure chain:
  1. the issue is bound to scene A with a non-zero scene_user_id;
  2. a cross-pipeline handoff switches it to scene B and
     `_refresh_issue_scene_after_handoff` calls `set_issue_scene(...)` without an
     owner → the default 0 overwrites the previous owner;
  3. a2a dispatch derives task.user_id from the issue scene → 0;
  4. a scope=user engine filters candidates by `t.user_id=<owner id>` in
     claim_by_tag, so it can never claim the task (event stuck in running, task
     stuck in queued, no log line at all).

Invariants locked here:
  A. handoff refresh backfills the owner from the target scene `created_by`;
  B. owner not resolvable → previous scene_user_id is preserved (never 0);
  C. an explicit 0 (really no owner) is still written as 0.
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

import json  # noqa: E402

import pytest  # noqa: E402

from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.api.db_setup import init_database  # noqa: E402

init_database()

from src.api.session_store import set_issue_scene  # noqa: E402
from src.domains.runtime.routes.webhook import _refresh_issue_scene_after_handoff  # noqa: E402
from src.api.pipeline_config import invalidate_scene_cache  # noqa: E402

REPO = "gitcode:handoff-uid/repo"
SCENE_OLD = "handoff-uid-old-scene"
SCENE_NEW = "handoff-uid-new-scene"
ISSUE_NO = 9910
OWNER_NAME = "handoff_uid_owner"
OWNER_ID = 991077
TASKS_NODES = [
    {"id": "qa", "tag": "qa", "skill": "qa-cli", "mode": "emit", "next": "done",
     "handoff": "a2a", "skip": [], "enabled": True},
]


def _purge():
    invalidate_scene_cache()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM grape_accounts WHERE id=? OR LOWER(username)=LOWER(?)",
                  (OWNER_ID, OWNER_NAME))
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _seed_scene(scene_id, created_by, repo_key=REPO):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active, created_by) "
            "VALUES (?,?,?,?,1,?)",
            (scene_id, scene_id, repo_key, json.dumps(TASKS_NODES), created_by))
        db.commit()


def _seed_account(uid, username):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO grape_accounts (id, username, nickname, status) VALUES (?,?,?,1)",
            (uid, username, username))
        db.commit()


def _seed_issue(scene_id, scene_user_id):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, scene_id, scene_user_id) "
            "VALUES (?,?,?,?,?,?)",
            (OWNER_ID, OWNER_NAME, REPO, ISSUE_NO, scene_id, str(scene_user_id)))
        db.commit()


def _issue_scene_row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT scene_id, scene_user_id FROM issue WHERE repo_key=? AND issue_number=?",
                  (REPO, ISSUE_NO))
        return c.fetchone()


def test_handoff_refresh_backfills_owner_from_scene_created_by():
    """A: target scene created_by resolves to an account id → issue owner is backfilled."""
    _seed_account(OWNER_ID, OWNER_NAME)
    _seed_scene(SCENE_NEW, OWNER_NAME)
    _seed_issue(SCENE_OLD, OWNER_ID)

    effective = _refresh_issue_scene_after_handoff(REPO, str(ISSUE_NO), SCENE_NEW)

    assert effective == SCENE_NEW
    scene_id, scene_uid = _issue_scene_row()
    assert scene_id == SCENE_NEW
    assert int(str(scene_uid).strip() or 0) == OWNER_ID


def test_handoff_refresh_preserves_uid_when_scene_owner_unresolvable():
    """B: created_by is not a registered account → previous owner is preserved, not 0."""
    _seed_scene(SCENE_NEW, "not-a-registered-account")
    _seed_issue(SCENE_OLD, OWNER_ID)

    effective = _refresh_issue_scene_after_handoff(REPO, str(ISSUE_NO), SCENE_NEW)

    assert effective == SCENE_NEW
    scene_id, scene_uid = _issue_scene_row()
    assert scene_id == SCENE_NEW
    assert int(str(scene_uid).strip() or 0) == OWNER_ID


def test_handoff_refresh_preserves_uid_when_created_by_empty():
    """B (legacy rows): empty created_by → previous owner is preserved as well."""
    _seed_scene(SCENE_NEW, "")
    _seed_issue(SCENE_OLD, OWNER_ID)

    _refresh_issue_scene_after_handoff(REPO, str(ISSUE_NO), SCENE_NEW)

    _, scene_uid = _issue_scene_row()
    assert int(str(scene_uid).strip() or 0) == OWNER_ID


def test_set_issue_scene_without_uid_keeps_previous_value():
    """set_issue_scene without scene_user_id only rewrites the scene, not the owner."""
    _seed_issue(SCENE_OLD, OWNER_ID)

    set_issue_scene(REPO, f"#{ISSUE_NO}", SCENE_NEW)

    scene_id, scene_uid = _issue_scene_row()
    assert scene_id == SCENE_NEW
    assert int(str(scene_uid).strip() or 0) == OWNER_ID


def test_set_issue_scene_explicit_zero_still_writes_zero():
    """C: explicit 0 means "really no owner" and is still written as 0."""
    _seed_issue(SCENE_OLD, OWNER_ID)

    set_issue_scene(REPO, f"#{ISSUE_NO}", SCENE_NEW, scene_user_id=0)

    _, scene_uid = _issue_scene_row()
    assert int(str(scene_uid).strip() or 0) == 0