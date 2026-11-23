"""Issue #2090: organization repository binding tests."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.kernel.session_manager import db, db_lock

UID = 22091
OTHER_UID = 22092
USERNAME = "issue2090-bind-owner"
OTHER_USERNAME = "issue2090-bind-other"
SLUG = "issue2090-bind"
TARGET_SLUG = "issue2090-target"
REPO_KEY = "gitcode:issue2090/bind-repo"
TARGET_REPO_KEY = "gitcode:issue2090/target-repo"
ORG_REPO_KEY = "gitcode:issue2090/org-managed"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _cleanup():
    with db_lock:
        c = db.cursor()
        org_ids = [
            r[0] for r in c.execute(
                "SELECT id FROM organization WHERE owner_id IN (?, ?)", (UID, OTHER_UID)
            ).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
        c.execute("DELETE FROM organization WHERE owner_id IN (?, ?)", (UID, OTHER_UID))
        for repo_key in (REPO_KEY, TARGET_REPO_KEY, ORG_REPO_KEY):
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (repo_key,))
        c.execute(
            "DELETE FROM distributed_locks WHERE lock_key LIKE 'org-repo-bind:%' "
            "OR lock_key LIKE 'repo-dispatch:%'"
        )
        db.commit()


def _seed_org(owner_id: int, slug: str, *, max_repos: int = 20, is_personal: int = 0) -> int:
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(slug=slug, name=slug, owner_id=owner_id, is_personal=is_personal)
    assert org
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE organization SET max_repos=? WHERE id=?", (max_repos, org["id"]))
        db.commit()
    return org["id"]


def _add_org_member(org_id: int, user_id: int, role: str = "member"):
    from src.domains.mgmt.user.org_db import add_org_member

    assert add_org_member(org_id, user_id, role)


def _seed_repo(repo_key: str, *, org_id: int = 0, is_org_repo: int = 0):
    from src.kernel.repo_key import parse_repo_key
    from src.domains.mgmt.user.db import add_managed_repo

    provider, owner, name = parse_repo_key(repo_key)
    add_managed_repo(repo_key, owner, name, webhook_id=1234, org_id=org_id)
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE managed_repos SET is_org_repo=? WHERE repo_key=?",
            (is_org_repo, repo_key),
        )
        db.commit()


@pytest.fixture
def client(monkeypatch):
    from src.domains.mgmt.routes import org_repos

    current = {"id": UID}

    def _fake_user():
        return {"id": current["id"], "username": USERNAME, "auth_type": "jwt"}

    monkeypatch.setattr(org_repos, "_require_repo_maintainer", lambda user, repo_key: None)
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    yield TestClient(app), current
    app.dependency_overrides.clear()


def test_bind_repo_writes_owner_quota_and_audit(client):
    api, _current = client
    org_id = _seed_org(UID, SLUG)
    _seed_repo(REPO_KEY)

    response = api.post(f"/api/orgs/{org_id}/repos", json={"repo_key": REPO_KEY})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repo_key"] == REPO_KEY
    assert body["is_org_repo"] is False

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT org_id, is_org_repo, linked_by, linked_at FROM managed_repos WHERE repo_key=?",
            (REPO_KEY,),
        ).fetchone()
        audit_count = c.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE event_type='org_repo_bind' "
            "AND detail LIKE ?", (f"%{REPO_KEY}%",)
        ).fetchone()[0]
    assert row[0] == org_id and row[1] == 0 and str(row[2]) == str(UID) and row[3]
    assert audit_count == 1


def test_repo_single_owner_conflict(client):
    api, _current = client
    source_org = _seed_org(UID, SLUG)
    target_org = _seed_org(OTHER_UID, TARGET_SLUG)
    _add_org_member(target_org, UID, "admin")
    _seed_repo(REPO_KEY, org_id=source_org)

    response = api.post(f"/api/orgs/{target_org}/repos", json={"repo_key": REPO_KEY})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "REPO_ALREADY_BOUND"


def test_repo_already_bound_to_same_org_is_idempotent(client):
    api, _current = client
    org_id = _seed_org(UID, SLUG)
    _seed_repo(REPO_KEY, org_id=org_id)

    with db_lock:
        c = db.cursor()
        audit_count_before = c.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE event_type='org_repo_bind' "
            "AND detail LIKE ?", (f"%{REPO_KEY}%",)
        ).fetchone()[0]

    response = api.post(f"/api/orgs/{org_id}/repos", json={"repo_key": REPO_KEY})
    assert response.status_code == 200, response.text
    assert response.json()["repo_key"] == REPO_KEY
    assert response.json()["org_id"] == org_id

    with db_lock:
        c = db.cursor()
        audit_count = c.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE event_type='org_repo_bind' "
            "AND detail LIKE ?", (f"%{REPO_KEY}%",)
        ).fetchone()[0]
    assert audit_count == audit_count_before


def test_personal_org_binding_is_treated_as_unbound(client):
    api, _current = client
    personal_org = _seed_org(UID, f"{SLUG}-personal", is_personal=1)
    team_org = _seed_org(UID, SLUG)
    _seed_repo(REPO_KEY, org_id=personal_org)

    response = api.post(f"/api/orgs/{team_org}/repos", json={"repo_key": REPO_KEY})
    assert response.status_code == 200, response.text
    assert response.json()["org_id"] == team_org

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT org_id, linked_by FROM managed_repos WHERE repo_key=?",
            (REPO_KEY,),
        ).fetchone()
    assert row[0] == team_org
    assert str(row[1]) == str(UID)


def test_repo_not_hosted_404(client):
    api, _current = client
    org_id = _seed_org(UID, SLUG)

    response = api.post(f"/api/orgs/{org_id}/repos", json={"repo_key": "gitcode:issue2090/missing"})
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error_code"] == "REPO_NOT_HOSTED"


def test_list_unbind_and_protected(client):
    api, _current = client
    org_id = _seed_org(UID, SLUG)
    _seed_repo(REPO_KEY, org_id=org_id)
    _seed_repo(ORG_REPO_KEY, org_id=org_id, is_org_repo=1)

    listing = api.get(f"/api/orgs/{org_id}/repos")
    assert listing.status_code == 200, listing.text
    assert {r["repo_key"] for r in listing.json()["repos"]} == {REPO_KEY, ORG_REPO_KEY}

    protected = api.request(
        "DELETE",
        f"/api/orgs/{org_id}/repos/{ORG_REPO_KEY}",
        json={"confirm": True},
    )
    assert protected.status_code == 409, protected.text
    assert protected.json()["detail"]["error_code"] == "ORG_REPO_PROTECTED"

    removed = api.request(
        "DELETE",
        f"/api/orgs/{org_id}/repos/{REPO_KEY}",
        json={"confirm": True},
    )
    assert removed.status_code == 200, removed.text
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT org_id, linked_by, linked_at FROM managed_repos WHERE repo_key=?",
            (REPO_KEY,),
        ).fetchone()
    assert row == ("", "", "")


def test_transfer_repo_requires_target_admin_and_updates_org(client):
    api, current = client
    source_org = _seed_org(UID, SLUG)
    target_org = _seed_org(OTHER_UID, TARGET_SLUG)
    _seed_repo(REPO_KEY, org_id=source_org)

    denied = api.post(
        f"/api/orgs/{source_org}/repos/{REPO_KEY}/transfer",
        json={"target_org_id": target_org},
    )
    assert denied.status_code == 403, denied.text

    _add_org_member(target_org, UID, "admin")
    moved = api.post(
        f"/api/orgs/{source_org}/repos/{REPO_KEY}/transfer",
        json={"target_org_id": target_org},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["org_id"] == target_org

    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT org_id FROM managed_repos WHERE repo_key=?", (REPO_KEY,)).fetchone()
    assert row[0] == target_org


def test_quota_blocks_new_binding(client):
    api, _current = client
    org_id = _seed_org(UID, SLUG, max_repos=1)
    _seed_repo(REPO_KEY, org_id=org_id)
    _seed_repo(TARGET_REPO_KEY)

    response = api.post(f"/api/orgs/{org_id}/repos", json={"repo_key": TARGET_REPO_KEY})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "ORG_REPO_QUOTA_EXCEEDED"


def test_transfer_quota_is_enforced_under_bind_lease(client):
    api, _current = client
    source_org = _seed_org(UID, SLUG)
    target_org = _seed_org(OTHER_UID, TARGET_SLUG, max_repos=1)
    _add_org_member(target_org, UID, "admin")
    _seed_repo(REPO_KEY, org_id=source_org)
    _seed_repo(TARGET_REPO_KEY, org_id=target_org)

    response = api.post(
        f"/api/orgs/{source_org}/repos/{REPO_KEY}/transfer",
        json={"target_org_id": target_org},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "ORG_REPO_QUOTA_EXCEEDED"

    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT org_id FROM managed_repos WHERE repo_key=?", (REPO_KEY,)).fetchone()
    assert row[0] == source_org


def test_migrate_endpoint_enforces_target_quota(client, monkeypatch):
    api, _current = client
    target_org = _seed_org(OTHER_UID, TARGET_SLUG, max_repos=1)
    _add_org_member(target_org, UID, "admin")
    _seed_repo(REPO_KEY)
    _seed_repo(TARGET_REPO_KEY, org_id=target_org)
    monkeypatch.setattr("src.api.deps.check_repo_access", lambda user, repo_key: None)

    response = api.post("/api/repos/migrate", json={
        "repo_key": REPO_KEY,
        "target_owner": TARGET_SLUG,
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "ORG_REPO_QUOTA_EXCEEDED"

    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT org_id FROM managed_repos WHERE repo_key=?", (REPO_KEY,)).fetchone()
    assert row[0] == ""


def test_migrate_endpoint_rejects_org_repo(client, monkeypatch):
    api, _current = client
    source_org = _seed_org(UID, SLUG)
    target_org = _seed_org(OTHER_UID, TARGET_SLUG)
    _add_org_member(target_org, UID, "admin")
    _seed_repo(REPO_KEY, org_id=source_org, is_org_repo=1)
    monkeypatch.setattr("src.api.deps.check_repo_access", lambda user, repo_key: None)

    response = api.post("/api/repos/migrate", json={
        "repo_key": REPO_KEY,
        "target_owner": TARGET_SLUG,
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "ORG_REPO_PROTECTED"

    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT org_id FROM managed_repos WHERE repo_key=?", (REPO_KEY,)).fetchone()
    assert row[0] == source_org


def test_migrate_endpoint_writes_transfer_audit(client, monkeypatch):
    api, _current = client
    source_org = _seed_org(UID, SLUG)
    target_org = _seed_org(OTHER_UID, TARGET_SLUG)
    _add_org_member(target_org, UID, "admin")
    _seed_repo(REPO_KEY, org_id=source_org)
    monkeypatch.setattr("src.api.deps.check_repo_access", lambda user, repo_key: None)

    response = api.post("/api/repos/migrate", json={
        "repo_key": REPO_KEY,
        "target_owner": TARGET_SLUG,
    })
    assert response.status_code == 200, response.text

    with db_lock:
        c = db.cursor()
        audit_rows = c.execute(
            "SELECT detail FROM audit_logs WHERE event_type='org_repo_transfer' "
            "AND detail LIKE ? ORDER BY seq DESC",
            (f"%{REPO_KEY}%",),
        ).fetchall()
    assert audit_rows
    assert "repos_migrate" in audit_rows[0][0]


def test_unbind_rejected_when_dispatch_is_running(client):
    api, _current = client
    org_id = _seed_org(UID, SLUG)
    _seed_repo(REPO_KEY, org_id=org_id)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks "
            "(id, agent_session_id, runtime_id, repo_key, status, prompt) "
            "VALUES (?, ?, ?, ?, 'running', ?)",
            ("issue2090-running-task", "issue2090-session", UID, REPO_KEY, "test"),
        )
        db.commit()

    response = api.request(
        "DELETE",
        f"/api/orgs/{org_id}/repos/{REPO_KEY}",
        json={"confirm": True},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "ORG_REPO_DISPATCH_RUNNING"

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT org_id FROM managed_repos WHERE repo_key=?",
            (REPO_KEY,),
        ).fetchone()
    assert row[0] == org_id


def test_unbind_uses_cross_instance_dispatch_lease(client, monkeypatch):
    from src.domains.mgmt.user import org_db
    from src.kernel.lock import repo_dispatch_lease_key

    api, _current = client
    org_id = _seed_org(UID, SLUG)
    _seed_repo(REPO_KEY, org_id=org_id)
    seen_keys = []

    def _acquire(key, ttl=30, attempts=20, interval=0.05):
        seen_keys.append(key)
        return key != repo_dispatch_lease_key(REPO_KEY)

    monkeypatch.setattr(org_db, "acquire_lock_with_retry", _acquire)
    monkeypatch.setattr(org_db, "release_lock", lambda key: None)

    response = api.request(
        "DELETE",
        f"/api/orgs/{org_id}/repos/{REPO_KEY}",
        json={"confirm": True},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "ORG_REPO_BUSY"
    assert repo_dispatch_lease_key(REPO_KEY) in seen_keys


def test_non_admin_or_non_maintainer_403(client, monkeypatch):
    from src.domains.mgmt.routes import org_repos

    api, current = client
    org_id = _seed_org(UID, SLUG)
    _seed_repo(REPO_KEY)

    current["id"] = OTHER_UID
    denied_membership = api.post(f"/api/orgs/{org_id}/repos", json={"repo_key": REPO_KEY})
    assert denied_membership.status_code == 403

    current["id"] = UID
    monkeypatch.setattr(
        org_repos,
        "_require_repo_maintainer",
        lambda user, repo_key: (_ for _ in ()).throw(
            org_repos._permission_error()
        ),
    )
    denied_vcs = api.post(f"/api/orgs/{org_id}/repos", json={"repo_key": REPO_KEY})
    assert denied_vcs.status_code == 403
    assert denied_vcs.json()["detail"]["error_code"] == "PERMISSION_DENIED"


def test_personal_org_binding_400(client):
    api, _current = client
    org_id = _seed_org(UID, SLUG, is_personal=1)
    _seed_repo(REPO_KEY)

    response = api.post(f"/api/orgs/{org_id}/repos", json={"repo_key": REPO_KEY})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error_code"] == "INVALID_ORG_CONTEXT"
