"""Issue #2263: 关联仓库多选列表 + 批量关联。

覆盖：
- GET /orgs/{id}/repos/available：返回该用户名下可关联仓库；
  排除其它团队组织仓库与组织自动仓库；包含未归属与个人组织仓库。
- POST /orgs/{id}/repos/batch：多选一次提交，逐个返回结果；重复 key 去重。
- 非管理员 403。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.kernel.session_manager import db, db_lock

UID = 22631
OTHER_UID = 22632
MEMBER_UID = 22633
USERNAME = "issue2263-owner"
SLUG = "issue2263-team"
OTHER_SLUG = "issue2263-other-team"

UNBOUND_KEY = "gitcode:issue2263/unbound"
PERSONAL_KEY = "gitcode:issue2263/personal"
OTHER_TEAM_KEY = "gitcode:issue2263/other-team"
AUTO_KEY = "gitcode:issue2263/org-auto"

ALL_KEYS = (UNBOUND_KEY, PERSONAL_KEY, OTHER_TEAM_KEY, AUTO_KEY)


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
                "SELECT id FROM organization WHERE owner_id IN (?, ?, ?)",
                (UID, OTHER_UID, MEMBER_UID),
            ).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM grape_t_org_dispatch WHERE org_id=?", (org_id,))
        c.execute(
            "DELETE FROM organization WHERE owner_id IN (?, ?, ?)",
            (UID, OTHER_UID, MEMBER_UID),
        )
        for key in ALL_KEYS:
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (key,))
            c.execute("DELETE FROM user_repos WHERE repo_key=?", (key,))
            c.execute("DELETE FROM audit_logs WHERE detail LIKE ?", (f"%{key}%",))
        db.commit()


def _seed_org(owner_id: int, slug: str, *, is_personal: int = 0) -> str:
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(name=slug, slug=slug, owner_id=owner_id, is_personal=is_personal)
    assert org
    return org["id"]


def _seed_repo(repo_key: str, *, org_id: str = "", is_org_repo: int = 0, owner_uid: int = UID) -> None:
    from src.domains.mgmt.user.db import add_managed_repo
    from src.kernel.repo_key import parse_repo_key

    _p, owner, name = parse_repo_key(repo_key)
    assert add_managed_repo(repo_key, owner, name, webhook_id=7000, org_id=org_id)
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE managed_repos SET is_org_repo=? WHERE repo_key=?", (is_org_repo, repo_key)
        )
        c.execute(
            "INSERT OR REPLACE INTO user_repos (repo_key, user_id, is_join, is_admin) "
            "VALUES (?, ?, 1, 1)",
            (repo_key, owner_uid),
        )
        db.commit()


def _client(monkeypatch, uid: int = UID, username: str = USERNAME):
    from src.domains.mgmt.routes import org_repos

    monkeypatch.setattr(org_repos, "_require_repo_maintainer", lambda user, repo_key: None)

    def _fake_user():
        return {"id": uid, "user_id": uid, "username": username, "auth_type": "jwt"}

    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    return TestClient(app)


def _close():
    app.dependency_overrides.pop(get_current_user_or_api_key, None)


def test_available_lists_user_repos_and_excludes_others(monkeypatch):
    org_id = _seed_org(UID, SLUG)
    other_team = _seed_org(OTHER_UID, OTHER_SLUG)
    personal = _seed_org(UID, f"{SLUG}-personal", is_personal=1)
    from src.domains.mgmt.user.org_db import update_org_provision_fields

    assert update_org_provision_fields(org_id, org_repo_key=AUTO_KEY, provision_status="ready")

    _seed_repo(UNBOUND_KEY)
    _seed_repo(PERSONAL_KEY, org_id=personal)
    _seed_repo(OTHER_TEAM_KEY, org_id=other_team)
    _seed_repo(AUTO_KEY, org_id=org_id, is_org_repo=1)

    api = _client(monkeypatch)
    try:
        r = api.get(f"/api/orgs/{org_id}/repos/available")
    finally:
        _close()

    assert r.status_code == 200, r.text
    keys = {item["repo_key"] for item in r.json()["repos"]}
    assert UNBOUND_KEY in keys
    assert PERSONAL_KEY in keys
    assert OTHER_TEAM_KEY not in keys, "其它团队组织的仓库不应出现"
    assert AUTO_KEY not in keys, "组织自动仓库不应出现"


def test_batch_bind_links_multiple_and_reports_failures(monkeypatch):
    org_id = _seed_org(UID, SLUG)
    _seed_repo(UNBOUND_KEY)
    _seed_repo(PERSONAL_KEY)

    api = _client(monkeypatch)
    try:
        r = api.post(
            f"/api/orgs/{org_id}/repos/batch",
            json={"repo_keys": [UNBOUND_KEY, PERSONAL_KEY, UNBOUND_KEY]},
        )
        listing = api.get(f"/api/orgs/{org_id}/repos")
    finally:
        _close()

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bound"] == 2 and body["failed"] == 0, body
    assert listing.status_code == 200
    assert {x["repo_key"] for x in listing.json()["repos"]} == {UNBOUND_KEY, PERSONAL_KEY}

    with db_lock:
        c = db.cursor()
        rows = {
            r[0]: r[1]
            for r in c.execute(
                "SELECT repo_key, org_id FROM managed_repos WHERE repo_key IN (?, ?)",
                (UNBOUND_KEY, PERSONAL_KEY),
            ).fetchall()
        }
    assert rows[UNBOUND_KEY] == org_id and rows[PERSONAL_KEY] == org_id


def test_batch_bind_requires_admin(monkeypatch):
    org_id = _seed_org(UID, SLUG)
    from src.domains.mgmt.user.org_db import add_org_member

    assert add_org_member(org_id, MEMBER_UID, "member")
    _seed_repo(UNBOUND_KEY)

    api = _client(monkeypatch, uid=MEMBER_UID, username="issue2263-member")
    try:
        r = api.post(f"/api/orgs/{org_id}/repos/batch", json={"repo_keys": [UNBOUND_KEY]})
    finally:
        _close()

    assert r.status_code == 403, r.text
    with db_lock:
        row = db.cursor().execute(
            "SELECT org_id FROM managed_repos WHERE repo_key=?", (UNBOUND_KEY,)
        ).fetchone()
    assert row[0] == ""