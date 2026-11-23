"""Issue #2261: 删除组织时回收组织仓库/场景（避免孤儿数据）。

覆盖：
- 组织自动仓库：删除 VCS 远端仓库 + 本地 managed_repos/user_repos 记录；
- 手工关联仓库：只退回「未归属」（org_id='' / is_org_repo=0），绝不删除远端；
- 组织场景行：随组织删除；
- 远端删除失败：不阻断组织删除，本地记录仍清理，错误回包可见；
- 个人组织：400 不可删。
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

UID = 22611
USERNAME = "issue2261-owner"
SLUG = "issue2261-team"
AUTO_REPO_KEY = "gitcode:issue2261/org-auto"
MANUAL_REPO_KEY = "gitcode:issue2261/manual"
SCENE_ID = "custom-org-2261"


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
                "SELECT id FROM organization WHERE owner_id=?", (UID,)
            ).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM grape_t_org_dispatch WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM grape_t_org_context WHERE org_id=?", (org_id,))
        c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
        for repo_key in (AUTO_REPO_KEY, MANUAL_REPO_KEY):
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM user_repos WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=?", (SCENE_ID,))
        db.commit()


def _seed_org(*, is_personal: int = 0) -> str:
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(
        name=SLUG, slug=SLUG, owner_id=UID, description="issue2261 delete", is_personal=is_personal
    )
    assert org
    return org["id"]


def _seed_repo(repo_key: str, *, org_id: str, is_org_repo: int, webhook_id: int) -> None:
    from src.domains.mgmt.user.db import add_managed_repo
    from src.kernel.repo_key import parse_repo_key

    _provider, owner, name = parse_repo_key(repo_key)
    assert add_managed_repo(repo_key, owner, name, webhook_id=webhook_id, org_id=org_id)
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE managed_repos SET is_org_repo=?, linked_by=?, linked_at=datetime('now') "
            "WHERE repo_key=?",
            (is_org_repo, UID, repo_key),
        )
        c.execute(
            "INSERT OR REPLACE INTO user_repos (repo_key, user_id, is_join, is_admin) "
            "VALUES (?, ?, 1, 1)",
            (repo_key, UID),
        )
        db.commit()


def _seed_scene(org_id: str) -> None:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, org_id) "
            "VALUES (?, ?, ?, '[]', 1, ?)",
            (SCENE_ID, "org scene", AUTO_REPO_KEY, org_id),
        )
        db.commit()


def _repo_row(repo_key: str):
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT org_id, COALESCE(is_org_repo, 0) FROM managed_repos WHERE repo_key=?",
            (repo_key,),
        ).fetchone()


def _client(monkeypatch):
    def _fake_user():
        return {"id": UID, "user_id": UID, "username": USERNAME, "auth_type": "jwt"}

    monkeypatch.setattr(
        "src.api.deps.get_user_token_with_refresh", lambda *a, **kw: "tk_owner", raising=False
    )
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    return TestClient(app)


def test_delete_team_org_purges_auto_repo_and_unbinds_manual(monkeypatch):
    from src.domains.mgmt.user.org_db import update_org_provision_fields

    org_id = _seed_org()
    assert update_org_provision_fields(
        org_id, org_repo_key=AUTO_REPO_KEY, org_scene_id=SCENE_ID, provision_status="ready"
    )
    _seed_repo(AUTO_REPO_KEY, org_id=org_id, is_org_repo=1, webhook_id=7001)
    _seed_repo(MANUAL_REPO_KEY, org_id=org_id, is_org_repo=0, webhook_id=7002)
    _seed_scene(org_id)

    deleted = []

    def _fake_delete(repo_key, token):
        deleted.append((repo_key, token))

    monkeypatch.setattr("src.domains.mgmt.org_provision._delete_vcs_repo", _fake_delete)

    api = _client(monkeypatch)
    try:
        r = api.delete(f"/api/orgs/{org_id}")
    finally:
        app.dependency_overrides.pop(get_current_user_or_api_key, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_remote"] == [AUTO_REPO_KEY]
    assert body["removed_local"] == [AUTO_REPO_KEY]
    assert body["unbound"] == [MANUAL_REPO_KEY]
    assert deleted == [(AUTO_REPO_KEY, "tk_owner")]

    from src.domains.mgmt.user.org_db import get_org

    assert get_org(org_id) is None
    # 自动仓库：本地记录清空（远程由 VCS 删除）
    assert _repo_row(AUTO_REPO_KEY) is None
    with db_lock:
        c = db.cursor()
        assert (
            c.execute(
                "SELECT COUNT(*) FROM user_repos WHERE repo_key=?", (AUTO_REPO_KEY,)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM devops_pipeline_scene WHERE org_id=?", (org_id,)
            ).fetchone()[0]
            == 0
        )
    # 手工关联仓库：退回未归属，远端保留
    assert _repo_row(MANUAL_REPO_KEY) == ("", 0)


def test_delete_org_remote_failure_still_cleans_local(monkeypatch):
    from src.domains.mgmt.user.org_db import update_org_provision_fields

    org_id = _seed_org()
    assert update_org_provision_fields(
        org_id, org_repo_key=AUTO_REPO_KEY, provision_status="ready"
    )
    _seed_repo(AUTO_REPO_KEY, org_id=org_id, is_org_repo=1, webhook_id=7101)

    def _boom(repo_key, token):
        raise RuntimeError("upstream 502")

    monkeypatch.setattr("src.domains.mgmt.org_provision._delete_vcs_repo", _boom)

    api = _client(monkeypatch)
    try:
        r = api.delete(f"/api/orgs/{org_id}")
    finally:
        app.dependency_overrides.pop(get_current_user_or_api_key, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_remote"] == []
    assert body["errors"] and "upstream 502" in body["errors"][0]
    # 远端失败不阻断：组织删除且本地孤儿记录清理
    assert _repo_row(AUTO_REPO_KEY) is None


def test_delete_personal_org_rejected(monkeypatch):
    org_id = _seed_org(is_personal=1)

    api = _client(monkeypatch)
    try:
        r = api.delete(f"/api/orgs/{org_id}")
    finally:
        app.dependency_overrides.pop(get_current_user_or_api_key, None)

    assert r.status_code == 400, r.text
    from src.domains.mgmt.user.org_db import get_org

    assert get_org(org_id) is not None

def test_delete_prefers_repo_level_token(monkeypatch):
    """仓库级凭据（通常 admin）优先于组织 owner 的用户 token；否则 GitHub 403。"""
    from src.domains.mgmt.user.org_db import update_org_provision_fields

    org_id = _seed_org()
    assert update_org_provision_fields(
        org_id, org_repo_key=AUTO_REPO_KEY, provision_status="ready"
    )
    _seed_repo(AUTO_REPO_KEY, org_id=org_id, is_org_repo=1, webhook_id=7201)

    deleted = []

    def _fake_delete(repo_key, token):
        deleted.append((repo_key, token))

    monkeypatch.setattr("src.domains.mgmt.org_provision._delete_vcs_repo", _fake_delete)
    monkeypatch.setattr(
        "src.domains.mgmt.user.db.get_repo_token_for", lambda repo_key: "repo-admin-token"
    )

    api = _client(monkeypatch)
    try:
        r = api.delete(f"/api/orgs/{org_id}")
    finally:
        app.dependency_overrides.pop(get_current_user_or_api_key, None)

    assert r.status_code == 200, r.text
    assert deleted == [(AUTO_REPO_KEY, "repo-admin-token")]
