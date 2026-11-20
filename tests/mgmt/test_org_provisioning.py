"""Issue #2090: organization provisioning state machine tests."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock

UID = 22090
USERNAME = "issue2090-owner"
SLUG = "issue2090-team"
REPO_KEY = "gitcode:issue2090/org-team"
BLOCKER_REPO_KEY = "gitcode:issue2090/quota-blocker"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM org_member WHERE org_id IN (SELECT id FROM organization WHERE owner_id=?)", (UID,))
        c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
        c.execute(
            "DELETE FROM managed_repos WHERE repo_key IN (?, ?)",
            (REPO_KEY, BLOCKER_REPO_KEY),
        )
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO_KEY,))
        c.execute(
            "DELETE FROM distributed_locks WHERE lock_key LIKE 'org-provision:%' "
            "OR lock_key LIKE 'org-repo-bind:%' OR lock_key LIKE 'repo-dispatch:%'"
        )
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM org_member WHERE org_id IN (SELECT id FROM organization WHERE owner_id=?)", (UID,))
        c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
        c.execute(
            "DELETE FROM managed_repos WHERE repo_key IN (?, ?)",
            (REPO_KEY, BLOCKER_REPO_KEY),
        )
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO_KEY,))
        c.execute(
            "DELETE FROM distributed_locks WHERE lock_key LIKE 'org-provision:%' "
            "OR lock_key LIKE 'org-repo-bind:%' OR lock_key LIKE 'repo-dispatch:%'"
        )
        db.commit()


def _seed_team_org(*, is_personal: int = 0, max_repos: int = 20) -> int:
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(
        name="Issue 2090 Team",
        slug=f"{SLUG}-{'personal' if is_personal else 'team'}",
        owner_id=UID,
        description="provisioning test",
        is_personal=is_personal,
    )
    assert org
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE organization SET max_repos=? WHERE id=?", (max_repos, org["id"]))
        db.commit()
    return org["id"]


def test_schema_has_provisioning_columns():
    from src.api import db_setup

    db_setup.init_database()
    with db_lock:
        c = db.cursor()
        org_cols = {r[1] for r in c.execute("PRAGMA table_info(organization)").fetchall()}
        repo_cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
    assert {"org_repo_key", "org_scene_id", "provision_status", "provision_error",
            "provisioned_at", "max_repos", "provision_attempt"} <= org_cols
    assert {"is_org_repo", "linked_by", "linked_at"} <= repo_cols
    with db_lock:
        c = db.cursor()
        task_indexes = {r[1] for r in c.execute("PRAGMA index_list(agent_tasks)").fetchall()}
    assert "idx_agent_tasks_repo_status" in task_indexes


def test_team_org_create_sets_pending_and_schedules(monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.deps import get_current_user_or_api_key
    from src.api.main import app
    from src.domains.mgmt.routes import orgs as orgs_route

    scheduled = []
    monkeypatch.setattr(orgs_route, "start_provision", lambda org_id: scheduled.append(org_id))
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": USERNAME, "auth_type": "jwt"
    }
    try:
        response = TestClient(app).post("/api/orgs", json={
            "name": "Issue 2090 Team",
            "slug": SLUG,
            "description": "team org",
        })
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_personal"] is False
    assert body["provision_status"] == "pending"
    assert scheduled == [body["id"]]


def test_personal_org_is_skipped():
    from src.domains.mgmt.org_provision import provision_org

    org_id = _seed_team_org(is_personal=1)
    result = provision_org(org_id)
    assert result["skipped"] is True
    assert result["provision_status"] == "none"


def _patch_successful_vcs(monkeypatch):
    import src.domains.mgmt.org_provision as provision

    calls = {"create": 0, "host": 0}

    def _fake_create_new_repo(body, user):
        calls["create"] += 1
        return {
            "repo": {"full_name": "issue2090/org-team", "name": "org-team"},
            "full_name": "issue2090/org-team",
            "owner": "issue2090",
            "name": "org-team",
            "provider": "gitcode",
            "hosted": False,
        }

    def _fake_host_repo(body, user, request=None, provider=""):
        calls["host"] += 1
        from src.domains.mgmt.user.db import add_managed_repo

        add_managed_repo(
            REPO_KEY,
            "issue2090",
            "org-team",
            webhook_id=4242,
            org_id=user["org_id"],
        )
        return {"message": "hosted", "webhook_id": 4242}

    monkeypatch.setattr(provision, "create_new_repo", _fake_create_new_repo)
    monkeypatch.setattr(provision, "host_repo", _fake_host_repo)
    return calls


def test_provision_reaches_ready_and_is_idempotent(monkeypatch):
    from src.domains.mgmt.org_provision import provision_org
    from src.domains.mgmt.user.org_db import get_org

    calls = _patch_successful_vcs(monkeypatch)
    org_id = _seed_team_org()

    first = provision_org(org_id)
    assert first["provision_status"] == "ready"
    assert calls == {"create": 1, "host": 1}

    org = get_org(org_id)
    assert org["org_repo_key"] == REPO_KEY
    assert org["org_scene_id"].startswith("custom-org-")
    assert org["provisioned_at"]

    with db_lock:
        c = db.cursor()
        repo = c.execute(
            "SELECT org_id, is_org_repo, linked_by, linked_at FROM managed_repos WHERE repo_key=?",
            (REPO_KEY,),
        ).fetchone()
        scene = c.execute(
            "SELECT nodes_json, advanced_options, org_id FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=?",
            (org["org_scene_id"], REPO_KEY),
        ).fetchone()
    assert repo and repo[0] == org_id and repo[1] == 1 and str(repo[2]) == str(UID) and repo[3]
    assert scene
    nodes = json.loads(scene[0])
    assert [n["id"] for n in nodes] == ["org_intake", "org_dispatch", "org_aggregate"]
    assert [n["skill"] for n in nodes] == ["org-intake-cli", "org-dispatch-cli", "org-aggregate-cli"]
    from src.domains.mgmt.org_context_repo import ORG_REPO_DEFAULT_BRANCH

    assert json.loads(scene[1])["work_branch"] == ORG_REPO_DEFAULT_BRANCH
    assert scene[2] == org_id

    second = provision_org(org_id)
    assert second["provision_status"] == "ready"
    assert calls == {"create": 1, "host": 1}


def test_provision_failure_is_retryable(monkeypatch):
    import src.domains.mgmt.org_provision as provision
    from src.domains.mgmt.user.org_db import get_org

    def _fail_create(body, user):
        raise RuntimeError("mock VCS unavailable")

    monkeypatch.setattr(provision, "create_new_repo", _fail_create)
    org_id = _seed_team_org()

    failed = provision.provision_org(org_id)
    assert failed["provision_status"] == "failed"
    assert "mock VCS unavailable" in failed["provision_error"]
    assert get_org(org_id)["provision_status"] == "failed"

    _patch_successful_vcs(monkeypatch)
    retried = provision.provision_org(org_id)
    assert retried["provision_status"] == "ready"


def test_provision_host_binding_enforces_org_quota(monkeypatch):
    from src.domains.mgmt.org_provision import provision_org
    from src.domains.mgmt.user.db import add_managed_repo
    from src.domains.mgmt.user.org_db import get_org

    _patch_successful_vcs(monkeypatch)
    org_id = _seed_team_org(max_repos=1)
    add_managed_repo(
        BLOCKER_REPO_KEY,
        "issue2090",
        "quota-blocker",
        webhook_id=1111,
        org_id=org_id,
    )

    result = provision_org(org_id)

    assert result["provision_status"] == "failed"
    assert "ORG_REPO_QUOTA_EXCEEDED" in result["provision_error"]
    assert get_org(org_id)["org_scene_id"] == ""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT org_id, COALESCE(is_org_repo, 0) FROM managed_repos WHERE repo_key=?",
            (REPO_KEY,),
        ).fetchone()
    assert row == ("", 0)


def _patch_claim_probe(monkeypatch, existing: dict, calls: dict):
    import src.domains.mgmt.org_provision as provision

    def _provider(username, login_provider="", user_id=0):
        return "gitcode", "test-token"

    def _check(name, user):
        calls.setdefault("probe", []).append(name)
        return existing

    monkeypatch.setattr(provision, "_get_user_provider_and_token", _provider)
    monkeypatch.setattr(provision, "check_repo_name", _check)


def _patch_failing_create(monkeypatch, calls: dict):
    import src.domains.mgmt.org_provision as provision

    def _fail_create(body, user):
        calls["create"] += 1
        raise RuntimeError("repository already exists")

    monkeypatch.setattr(provision, "create_new_repo", _fail_create)


def _patch_claim_host(monkeypatch, calls: dict):
    import src.domains.mgmt.org_provision as provision

    def _host(body, user, request=None, provider=""):
        calls["host"] += 1
        from src.domains.mgmt.user.db import add_managed_repo

        add_managed_repo(
            REPO_KEY,
            "issue2090",
            "org-team",
            webhook_id=4242,
            org_id=user["org_id"],
        )
        return {"message": "hosted", "webhook_id": 4242}

    monkeypatch.setattr(provision, "host_repo", _host)


def test_retry_claims_repo_created_before_crash(monkeypatch):
    from src.domains.mgmt.org_provision import provision_org
    from src.domains.mgmt.user.org_db import get_org

    calls = {"create": 0, "host": 0}
    _patch_failing_create(monkeypatch, calls)
    _patch_claim_probe(monkeypatch, {
        "exists": True,
        "full_name": "issue2090/org-team",
        "private": True,
        "description": "Managed repository for organization Issue 2090 Team",
    }, calls)
    _patch_claim_host(monkeypatch, calls)

    org_id = _seed_team_org()
    result = provision_org(org_id)

    assert result["provision_status"] == "ready"
    assert calls == {"create": 1, "host": 1, "probe": ["issue2090-team-team"]}
    assert get_org(org_id)["org_repo_key"] == REPO_KEY


def test_retry_claim_miss_keeps_original_error(monkeypatch):
    from src.domains.mgmt.org_provision import provision_org
    from src.domains.mgmt.user.org_db import get_org

    calls = {"create": 0, "host": 0}
    _patch_failing_create(monkeypatch, calls)
    _patch_claim_probe(monkeypatch, {"exists": False}, calls)
    _patch_claim_host(monkeypatch, calls)

    org_id = _seed_team_org()
    result = provision_org(org_id)

    assert result["provision_status"] == "failed"
    assert "repository already exists" in result["provision_error"]
    assert calls == {"create": 1, "host": 0, "probe": ["issue2090-team-team"]}
    assert get_org(org_id)["org_repo_key"] == ""


def test_retry_claim_rejects_repo_without_ownership_evidence(monkeypatch):
    from src.domains.mgmt.org_provision import provision_org
    from src.domains.mgmt.user.org_db import get_org

    calls = {"create": 0, "host": 0}
    _patch_failing_create(monkeypatch, calls)
    _patch_claim_probe(monkeypatch, {
        "exists": True,
        "full_name": "issue2090/org-team",
        "private": False,
        "description": "user owned repo",
    }, calls)
    _patch_claim_host(monkeypatch, calls)

    org_id = _seed_team_org()
    result = provision_org(org_id)

    assert result["provision_status"] == "failed"
    assert "repository already exists" in result["provision_error"]
    assert calls == {"create": 1, "host": 0, "probe": ["issue2090-team-team"]}
    assert get_org(org_id)["org_repo_key"] == ""


def test_lease_lost_marks_attempt_failed(monkeypatch):
    import src.domains.mgmt.org_provision as provision
    from src.domains.mgmt.user.org_db import get_org

    calls = _patch_successful_vcs(monkeypatch)
    renew_calls = {"count": 0}

    def _renew_then_lose(key, ttl=0):
        renew_calls["count"] += 1
        return renew_calls["count"] < 3

    monkeypatch.setattr(provision, "renew_lock", _renew_then_lose)

    org_id = _seed_team_org()
    result = provision.provision_org(org_id)

    assert result["provision_status"] == "failed"
    assert result["provision_error"] == "ORG_PROVISION_LEASE_LOST"
    org = get_org(org_id)
    assert org["provision_status"] == "failed"
    assert org["provision_error"] == "ORG_PROVISION_LEASE_LOST"
    assert org["provision_attempt"]
    assert calls["create"] == 1
    assert calls["host"] == 0


def test_host_failure_retry_does_not_recreate_repo(monkeypatch):
    import src.domains.mgmt.org_provision as provision

    calls = {"create": 0, "host": 0}

    def _create(body, user):
        calls["create"] += 1
        return {
            "repo": {"full_name": "issue2090/org-team", "name": "org-team"},
            "full_name": "issue2090/org-team",
            "owner": "issue2090",
            "name": "org-team",
            "provider": "gitcode",
            "hosted": False,
        }

    def _fail_host(body, user, request=None, provider=""):
        calls["host"] += 1
        raise RuntimeError("hosting unavailable")

    monkeypatch.setattr(provision, "create_new_repo", _create)
    monkeypatch.setattr(provision, "host_repo", _fail_host)
    org_id = _seed_team_org()

    first = provision.provision_org(org_id)
    assert first["provision_status"] == "failed"
    assert calls == {"create": 1, "host": 1}

    def _host(body, user, request=None, provider=""):
        calls["host"] += 1
        from src.domains.mgmt.user.db import add_managed_repo

        add_managed_repo(
            REPO_KEY,
            "issue2090",
            "org-team",
            webhook_id=4242,
            org_id=user["org_id"],
        )
        return {"message": "hosted", "webhook_id": 4242}

    monkeypatch.setattr(provision, "host_repo", _host)
    second = provision.provision_org(org_id)

    assert second["provision_status"] == "ready"
    assert calls == {"create": 1, "host": 2}


def test_host_retry_preserves_existing_target_binding(monkeypatch):
    import src.domains.mgmt.org_provision as provision
    from src.domains.mgmt.user.db import add_managed_repo

    _patch_successful_vcs(monkeypatch)
    org_id = _seed_team_org(max_repos=1)
    add_managed_repo(
        REPO_KEY,
        "issue2090",
        "org-team",
        webhook_id=0,
        org_id=org_id,
    )
    add_managed_repo(
        BLOCKER_REPO_KEY,
        "issue2090",
        "quota-blocker",
        webhook_id=1111,
        org_id=org_id,
    )
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET is_org_repo=1 WHERE repo_key=?", (REPO_KEY,))
        db.commit()

    result = provision.provision_org(org_id)

    assert result["provision_status"] == "ready"
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT org_id, COALESCE(is_org_repo, 0) FROM managed_repos WHERE repo_key=?",
            (REPO_KEY,),
        ).fetchone()
    assert row == (org_id, 1)


def test_provision_respects_db_lease(monkeypatch):
    import src.domains.mgmt.org_provision as provision
    from src.domains.mgmt.user.org_db import get_org

    calls = _patch_successful_vcs(monkeypatch)
    org_id = _seed_team_org()
    provision.update_org_provision_fields(org_id, provision_status="pending")
    monkeypatch.setattr(provision, "acquire_lock", lambda key, ttl=0: False)

    result = provision.provision_org(org_id)

    assert result["provision_status"] == "pending"
    assert calls == {"create": 0, "host": 0}
    assert get_org(org_id)["provision_status"] == "pending"


def test_start_provision_runs_in_background(monkeypatch):
    import src.domains.mgmt.org_provision as provision

    seen = []

    def _fake(org_id):
        time.sleep(0.02)
        seen.append(org_id)
        return {"provision_status": "ready"}

    monkeypatch.setattr(provision, "provision_org", _fake)
    provision.start_provision(str(UID + 1))
    deadline = time.time() + 1
    while time.time() < deadline and not seen:
        time.sleep(0.01)
    assert seen == [str(UID + 1)]


def test_provision_and_retry_endpoints(monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.deps import get_current_user_or_api_key
    from src.api.main import app
    from src.domains.mgmt.routes import orgs as orgs_route

    org_id = _seed_team_org()
    scheduled = []
    monkeypatch.setattr(orgs_route, "start_provision", lambda oid: scheduled.append(oid))
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": USERNAME, "auth_type": "jwt"
    }
    client = TestClient(app)
    try:
        response = client.get(f"/api/orgs/{org_id}/provision")
        assert response.status_code == 200, response.text
        assert response.json()["provision_status"] in ("none", "pending", "failed", "ready")

        retry = client.post(f"/api/orgs/{org_id}/provision/retry")
        assert retry.status_code == 200, retry.text
        assert retry.json()["provision_status"] == "pending"
        assert scheduled == [org_id]
    finally:
        app.dependency_overrides.clear()


def test_retry_ready_does_not_reset_status(monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.deps import get_current_user_or_api_key
    from src.api.main import app
    from src.domains.mgmt.routes import orgs as orgs_route

    org_id = _seed_team_org()
    orgs_route.update_org_provision_fields(org_id, provision_status="ready")
    scheduled = []
    monkeypatch.setattr(orgs_route, "start_provision", lambda oid: scheduled.append(oid))
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": USERNAME, "auth_type": "jwt"
    }
    client = TestClient(app)
    try:
        response = client.post(f"/api/orgs/{org_id}/provision/retry")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["provision_status"] == "ready"
    assert scheduled == []


def test_default_org_repo_name_has_no_prefix():
    """新建组织仓库默认名 = slug，不再自动加 org- 前缀。"""
    from src.domains.mgmt.org_provision import _repo_name

    assert _repo_name({"slug": "123", "id": "uuid-1"}) == "123"
    assert _repo_name({"slug": "Test Team", "id": "uuid-2"}) == "test-team"
    assert _repo_name({"slug": "", "id": "42"}) == "repo-42"


def test_provision_schedules_context_sync(monkeypatch):
    """provisioning 只调度 org-context 同步；分析分支在默认分支落定后由它接力创建。"""
    import src.domains.mgmt.org_context_repo as org_context_repo
    from src.domains.mgmt.org_provision import provision_org

    calls = []
    monkeypatch.setattr(org_context_repo, "schedule_sync",
                        lambda org_id, delay=0: calls.append(("context", str(org_id))))
    _patch_successful_vcs(monkeypatch)
    org_id = _seed_team_org()

    result = provision_org(org_id)
    assert result["provision_status"] == "ready"
    assert ("context", str(org_id)) in calls
