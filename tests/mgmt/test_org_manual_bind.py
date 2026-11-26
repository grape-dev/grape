"""Issue #2258: 手动绑定已有托管仓库为组织仓库（provisioning 兜底）测试。"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.kernel.session_manager import db, db_lock

UID = 22258
OTHER_UID = 22259
MEMBER_UID = 22260
STRANGER_UID = 22261
USERNAME = "issue2258-owner"
SLUG = "issue2258-team"
OTHER_SLUG = "issue2258-other"

MANAGED_REPO_KEY = "gitcode:issue2258/managed-repo"
REPLACEMENT_REPO_KEY = "gitcode:issue2258/replacement-repo"
OTHER_ORG_REPO_KEY = "gitcode:issue2258/other-org-repo"
MISSING_REPO_KEY = "gitcode:issue2258/missing-repo"
ALL_REPO_KEYS = (
    MANAGED_REPO_KEY,
    REPLACEMENT_REPO_KEY,
    OTHER_ORG_REPO_KEY,
    MISSING_REPO_KEY,
)
ALL_ORG_OWNERS = (UID, OTHER_UID, MEMBER_UID, STRANGER_UID)


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _cleanup() -> None:
    with db_lock:
        c = db.cursor()
        org_ids = [
            r[0]
            for r in c.execute(
                "SELECT id FROM organization WHERE owner_id IN (?, ?, ?, ?)",
                ALL_ORG_OWNERS,
            ).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM grape_t_org_dispatch WHERE org_id=?", (org_id,))
        c.execute(
            "DELETE FROM organization WHERE owner_id IN (?, ?, ?, ?)", ALL_ORG_OWNERS
        )
        for repo_key in ALL_REPO_KEYS:
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
            c.execute(
                "DELETE FROM audit_logs WHERE detail LIKE ?", (f"%{repo_key}%",)
            )
        db.commit()


def _seed_org(owner_id: int = UID, slug: str = SLUG, *, is_personal: int = 0) -> int:
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(
        name=slug,
        slug=slug,
        owner_id=owner_id,
        description="issue2258 manual bind",
        is_personal=is_personal,
    )
    assert org
    return org["id"]


def _add_member(org_id: int, user_id: int, role: str = "member") -> None:
    from src.domains.mgmt.user.org_db import add_org_member

    assert add_org_member(org_id, user_id, role)


def _seed_repo(repo_key: str, *, org_id: int = 0, webhook_id: int = 4321) -> None:
    from src.domains.mgmt.user.db import add_managed_repo
    from src.kernel.repo_key import parse_repo_key

    _provider, owner, name = parse_repo_key(repo_key)
    assert add_managed_repo(repo_key, owner, name, webhook_id=webhook_id, org_id=org_id)


def _org_row(org_id: int) -> dict:
    from src.domains.mgmt.user.org_db import get_org

    org = get_org(org_id)
    assert org
    return org


def _repo_row(repo_key: str) -> tuple:
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT org_id, is_org_repo, linked_by, linked_at "
            "FROM managed_repos WHERE repo_key=?",
            (repo_key,),
        ).fetchone()


def _audit_rows(repo_key: str) -> list[tuple]:
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT event_type, detail FROM audit_logs "
            "WHERE event_type='org_provision_manual_bind' AND detail LIKE ?",
            (f"%{repo_key}%",),
        ).fetchall()


def _bind(api: TestClient, org_id: int, repo_key: str, *, confirm: bool | None = None):
    body = {"repo_key": repo_key}
    if confirm is not None:
        body["confirm"] = confirm
    return api.put(f"/api/orgs/{org_id}/provision/repo", json=body)


@pytest.fixture
def client():
    current = {"id": UID}

    def _fake_user():
        return {"id": current["id"], "username": USERNAME, "auth_type": "jwt"}

    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    yield TestClient(app), current
    app.dependency_overrides.clear()


def test_manual_bind_success_writes_repo_scene_and_audit(client):
    api, _current = client
    org_id = _seed_org()
    _seed_repo(MANAGED_REPO_KEY)

    response = _bind(api, org_id, MANAGED_REPO_KEY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["org_repo_key"] == MANAGED_REPO_KEY
    assert body["provision_status"] == "ready"
    assert body["provision_error"] == ""
    assert body["provisioned_at"]
    assert body["org_scene_id"].startswith("custom-org-")

    org = _org_row(org_id)
    assert org["org_repo_key"] == MANAGED_REPO_KEY
    assert org["org_scene_id"] == body["org_scene_id"]
    assert org["provision_status"] == "ready"
    assert not org["provision_error"]

    repo = _repo_row(MANAGED_REPO_KEY)
    assert repo[0] == org_id and repo[1] == 1 and str(repo[2]) == str(UID) and repo[3]

    with db_lock:
        c = db.cursor()
        scene = c.execute(
            "SELECT nodes_json, org_id FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=?",
            (org["org_scene_id"], MANAGED_REPO_KEY),
        ).fetchone()
    assert scene and scene[1] == org_id
    nodes = json.loads(scene[0])
    assert [n["id"] for n in nodes] == ["org_vod", "org_intake", "org_dispatch", "org_aggregate"]

    audits = _audit_rows(MANAGED_REPO_KEY)
    assert len(audits) == 1
    assert audits[0][0] == "org_provision_manual_bind"
    detail = json.loads(audits[0][1])
    assert detail["org_id"] == org_id
    assert detail["repo_key"] == MANAGED_REPO_KEY
    assert detail["replaced"] is False


def test_manual_bind_accepts_unbound_hosted_repo(client):
    api, _current = client
    org_id = _seed_org()
    _seed_repo(MANAGED_REPO_KEY, org_id=0)

    response = _bind(api, org_id, MANAGED_REPO_KEY)
    assert response.status_code == 200, response.text
    assert _org_row(org_id)["org_repo_key"] == MANAGED_REPO_KEY
    assert _repo_row(MANAGED_REPO_KEY)[0] == org_id


def test_manual_bind_other_org_conflict_409(client):
    api, _current = client
    other_org_id = _seed_org(OTHER_UID, OTHER_SLUG)
    org_id = _seed_org()
    _seed_repo(OTHER_ORG_REPO_KEY, org_id=other_org_id)

    response = _bind(api, org_id, OTHER_ORG_REPO_KEY)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "REPO_ALREADY_BOUND"
    assert _org_row(org_id)["org_repo_key"] == ""
    assert _repo_row(OTHER_ORG_REPO_KEY)[0] == other_org_id


def test_manual_bind_not_hosted_404(client):
    api, _current = client
    org_id = _seed_org()

    response = _bind(api, org_id, MISSING_REPO_KEY)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error_code"] == "REPO_NOT_HOSTED"


def test_manual_bind_org_not_found_404(client):
    api, _current = client

    response = _bind(api, 9876543, MANAGED_REPO_KEY)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error_code"] == "ORG_NOT_FOUND"


def test_manual_bind_invalid_repo_400(client):
    api, _current = client
    org_id = _seed_org()
    _seed_repo(MANAGED_REPO_KEY)

    empty = _bind(api, org_id, "")
    assert empty.status_code == 400, empty.text
    assert empty.json()["detail"]["error_code"] == "INVALID_REPO"

    malformed = _bind(api, org_id, "not-a-repo")
    assert malformed.status_code == 400, malformed.text
    assert malformed.json()["detail"]["error_code"] == "INVALID_REPO"


def test_manual_bind_permission_denied_403(client):
    api, current = client
    org_id = _seed_org()
    _seed_repo(MANAGED_REPO_KEY)
    _add_member(org_id, MEMBER_UID, "member")

    current["id"] = MEMBER_UID
    member = _bind(api, org_id, MANAGED_REPO_KEY)
    assert member.status_code == 403, member.text
    assert member.json()["detail"]["error_code"] == "PERMISSION_DENIED"

    current["id"] = STRANGER_UID
    stranger = _bind(api, org_id, MANAGED_REPO_KEY)
    assert stranger.status_code == 403, stranger.text
    assert stranger.json()["detail"]["error_code"] == "PERMISSION_DENIED"


def test_manual_bind_personal_org_rejected_400(client):
    api, _current = client
    org_id = _seed_org(is_personal=1)
    _seed_repo(MANAGED_REPO_KEY)

    response = _bind(api, org_id, MANAGED_REPO_KEY)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error_code"] == "INVALID_ORG_CONTEXT"


def test_manual_bind_replacement_requires_confirm(client):
    api, _current = client
    org_id = _seed_org()
    _seed_repo(MANAGED_REPO_KEY)
    _seed_repo(REPLACEMENT_REPO_KEY)

    first = _bind(api, org_id, MANAGED_REPO_KEY)
    assert first.status_code == 200, first.text
    scene_id = first.json()["org_scene_id"]

    denied = _bind(api, org_id, REPLACEMENT_REPO_KEY)
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["error_code"] == "CONFIRM_REQUIRED"
    assert _org_row(org_id)["org_repo_key"] == MANAGED_REPO_KEY
    assert _repo_row(REPLACEMENT_REPO_KEY)[0] == ""

    replaced = _bind(api, org_id, REPLACEMENT_REPO_KEY, confirm=True)
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    assert body["org_repo_key"] == REPLACEMENT_REPO_KEY
    assert body["org_scene_id"] == scene_id  # 既有场景不重建
    assert body["provision_status"] == "ready"

    org = _org_row(org_id)
    assert org["org_repo_key"] == REPLACEMENT_REPO_KEY
    assert org["org_scene_id"] == scene_id

    old_repo = _repo_row(MANAGED_REPO_KEY)
    new_repo = _repo_row(REPLACEMENT_REPO_KEY)
    assert old_repo[0] == org_id and old_repo[1] == 0  # 旧仓库仍归属组织但取消组织仓库标记
    assert new_repo[0] == org_id and new_repo[1] == 1

    audits = _audit_rows(REPLACEMENT_REPO_KEY)
    assert len(audits) == 1
    detail = json.loads(audits[0][1])
    assert detail["replaced"] is True
    assert detail["previous_repo_key"] == MANAGED_REPO_KEY

    with db_lock:
        c = db.cursor()
        scene_count = c.execute(
            "SELECT COUNT(*) FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
            (scene_id, REPLACEMENT_REPO_KEY),
        ).fetchone()[0]
    assert scene_count == 0  # 替换不新建/不迁移场景


def test_manual_bind_replacement_blocked_by_running_dispatch(client):
    api, _current = client
    org_id = _seed_org()
    _seed_repo(MANAGED_REPO_KEY)
    _seed_repo(REPLACEMENT_REPO_KEY)

    first = _bind(api, org_id, MANAGED_REPO_KEY)
    assert first.status_code == 200, first.text

    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO grape_t_org_dispatch "
            "(id, org_id, org_issue_number, org_repo_key, scene_id, status) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (uuid.uuid4().hex, org_id, 1, MANAGED_REPO_KEY,
             first.json()["org_scene_id"]),
        )
        db.commit()

    blocked = _bind(api, org_id, REPLACEMENT_REPO_KEY, confirm=True)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["error_code"] == "ORG_REPO_EXISTS"
    assert _org_row(org_id)["org_repo_key"] == MANAGED_REPO_KEY

    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch SET status='completed' WHERE org_id=? AND org_repo_key=?",
            (org_id, MANAGED_REPO_KEY),
        )
        db.commit()

    allowed = _bind(api, org_id, REPLACEMENT_REPO_KEY, confirm=True)
    assert allowed.status_code == 200, allowed.text
    assert _org_row(org_id)["org_repo_key"] == REPLACEMENT_REPO_KEY


def test_auto_provision_after_manual_bind_is_noop(client, monkeypatch):
    import src.domains.mgmt.org_provision as provision

    api, _current = client
    org_id = _seed_org()
    _seed_repo(MANAGED_REPO_KEY)

    first = _bind(api, org_id, MANAGED_REPO_KEY)
    assert first.status_code == 200, first.text

    def _fail(*_args, **_kwargs):
        raise RuntimeError("VCS must not be touched after manual bind")

    monkeypatch.setattr(provision, "create_new_repo", _fail)
    monkeypatch.setattr(provision, "host_repo", _fail)

    result = provision.provision_org(org_id)
    assert result["provision_status"] == "ready"
    assert result["org_repo_key"] == MANAGED_REPO_KEY
    assert result["org_scene_id"] == first.json()["org_scene_id"]


def test_manual_bind_scene_failure_resets_placeholder_scene_id(client, monkeypatch):
    import src.domains.mgmt.org_provision as provision

    api, _current = client
    org_id = _seed_org()
    _seed_repo(MANAGED_REPO_KEY)
    real_create = provision.create_org_scene

    def _boom(**_kwargs):
        raise RuntimeError("scene backend down")

    monkeypatch.setattr(provision, "create_org_scene", _boom)
    failed = _bind(api, org_id, MANAGED_REPO_KEY)
    assert failed.status_code == 500, failed.text
    assert failed.json()["detail"]["error_code"] == "SCENE_CREATE_FAILED"

    org = _org_row(org_id)
    assert org["org_scene_id"] == ""  # 失败不留占位 scene_id，保证可重试
    assert org["org_repo_key"] == ""
    assert org["provision_status"] != "ready"
    assert _repo_row(MANAGED_REPO_KEY)[0] == ""

    monkeypatch.setattr(provision, "create_org_scene", real_create)
    retry = _bind(api, org_id, MANAGED_REPO_KEY)
    assert retry.status_code == 200, retry.text
    assert retry.json()["org_scene_id"].startswith("custom-org-")
    assert retry.json()["provision_status"] == "ready"