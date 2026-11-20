"""组织仓库创建选项：平台 / 所属空间 / 名称 / 路径 / 可见性。

覆盖：
- organization 表新增的 org_repo_* 列；
- GET /api/orgs/repo-namespaces 返回已绑定账号与可建仓空间；
- POST /api/orgs 持久化用户选择并在 provisioning 时传给 VCS 建仓；
- 指定平台但未绑定凭据时明确 400（不落组织）。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock

UID = 22950
USERNAME = "issue-org-repo-owner"
SLUG = "issue-org-repo-options"
REPO_KEY = "github:issue-org-repo/org-issue-org-repo-options"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    def _purge():
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM org_member WHERE org_id IN "
                "(SELECT id FROM organization WHERE owner_id=?)",
                (UID,),
            )
            c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO_KEY,))
            c.execute(
                "DELETE FROM distributed_locks WHERE lock_key LIKE 'org-provision:%' "
                "OR lock_key LIKE 'org-repo-bind:%' OR lock_key LIKE 'repo-dispatch:%'"
            )
            db.commit()

    _purge()
    yield
    _purge()


def _api():
    from fastapi.testclient import TestClient

    from src.api.deps import get_current_user_or_api_key
    from src.api.main import app

    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID,
        "username": USERNAME,
        "auth_type": "jwt",
    }
    return TestClient(app), app


def test_schema_has_org_repo_create_columns():
    with db_lock:
        c = db.cursor()
        cols = {r[1] for r in c.execute("PRAGMA table_info(organization)").fetchall()}
    assert {
        "org_repo_provider",
        "org_repo_owner",
        "org_repo_name",
        "org_repo_path",
        "org_repo_private",
    } <= cols


def test_repo_namespaces_lists_account_and_orgs(monkeypatch):
    from src.domains.mgmt.routes import orgs as orgs_route
    from src.domains.mgmt.user import repo_manager

    def _tokens(username, user_id="", provider="gitcode"):
        return "tk-gitcode" if provider == "gitcode" else ""

    class _Provider:
        def verify_token(self, token):
            assert token == "tk-gitcode"
            return {"login": "alice", "name": "Alice", "avatar_url": "https://a/1.png"}

    monkeypatch.setattr(orgs_route, "get_user_token_for", _tokens)
    monkeypatch.setattr("src.kernel.token_resolver.create_provider", lambda p: _Provider())
    monkeypatch.setattr(
        repo_manager,
        "fetch_org_namespaces",
        lambda provider, token: [
            {"login": "acme", "name": "Acme", "avatar_url": ""},
            {"login": "alice", "name": "Alice", "avatar_url": ""},
        ],
    )

    client, app = _api()
    try:
        response = client.get("/api/orgs/repo-namespaces")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    providers = {p["provider"]: p for p in response.json()["providers"]}
    assert providers["gitcode"]["connected"] is True
    assert providers["gitcode"]["account"]["login"] == "alice"
    assert [ns["login"] for ns in providers["gitcode"]["namespaces"]] == ["alice", "acme"]
    assert [ns["type"] for ns in providers["gitcode"]["namespaces"]] == ["user", "org"]
    assert providers["github"]["connected"] is False
    assert providers["github"]["namespaces"] == []


def test_create_org_persists_repo_options(monkeypatch):
    from src.domains.mgmt.routes import orgs as orgs_route
    from src.domains.mgmt.user.org_db import get_org

    scheduled = []
    monkeypatch.setattr(orgs_route, "start_provision", lambda org_id: scheduled.append(org_id))
    monkeypatch.setattr(orgs_route, "get_user_token_for", lambda *a, **kw: "tk")

    client, app = _api()
    try:
        response = client.post("/api/orgs", json={
            "name": "Repo Options Team",
            "slug": SLUG,
            "description": "create options",
            "repo_provider": "github",
            "repo_owner": "acme",
            "repo_name": "team-workspace",
            "repo_path": "",
            "repo_private": True,
        })
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["org_repo_provider"] == "github"
    assert body["org_repo_owner"] == "acme"
    assert body["org_repo_name"] == "team-workspace"
    assert body["org_repo_private"] is True
    assert scheduled == [body["id"]]

    org = get_org(body["id"])
    assert org["org_repo_provider"] == "github"
    assert org["org_repo_owner"] == "acme"
    assert org["org_repo_name"] == "team-workspace"
    assert int(org["org_repo_private"]) == 1


def test_create_org_rejects_unbound_provider(monkeypatch):
    from src.domains.mgmt.routes import orgs as orgs_route

    monkeypatch.setattr(orgs_route, "get_user_token_for", lambda *a, **kw: "")

    client, app = _api()
    try:
        response = client.post("/api/orgs", json={
            "name": "Unbound Provider Team",
            "slug": SLUG + "-unbound",
            "repo_provider": "github",
            "repo_owner": "acme",
        })
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error_code"] == "VCS_TOKEN_MISSING"
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT COUNT(*) FROM organization WHERE owner_id=?", (UID,)
        ).fetchone()
    assert row[0] == 0


def test_gitcode_create_repo_uses_org_namespace(monkeypatch):
    from src.domains.mgmt.user import repo_manager

    captured = {}

    def _fake_req(method, url, token="", body=None):
        captured.update({"method": method, "url": url, "body": body})
        return {"full_name": "acme/team-path"}

    monkeypatch.setattr(repo_manager, "_req", _fake_req)

    result = repo_manager.create_repo(
        "tk", "team-workspace", "desc", private=True, owner="acme", path="team-path")

    assert result["full_name"] == "acme/team-path"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/orgs/acme/repos")
    assert captured["body"]["name"] == "team-workspace"
    assert captured["body"]["path"] == "team-path"
    assert captured["body"]["private"] is True


def test_github_create_repo_uses_org_namespace(monkeypatch):
    from src.domains.mgmt.user import repo_manager

    captured = {}

    def _fake_req(method, url, token="", body=None):
        captured.update({"method": method, "url": url, "body": body})
        return {"full_name": "acme/team-workspace"}

    monkeypatch.setattr(repo_manager, "_github_req", _fake_req)

    result = repo_manager.create_github_repo(
        "tk", "team-workspace", "desc", private=True, owner="acme", path="ignored")

    assert result["full_name"] == "acme/team-workspace"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/orgs/acme/repos")
    assert captured["body"]["name"] == "team-workspace"
    assert captured["body"]["private"] is True


def test_github_create_repo_personal_namespace_uses_user_repos(monkeypatch):
    """选中的 owner 是 token 用户本人 → 个人空间 /user/repos。

    回归：创建组织弹窗默认选中「个人」空间，旧实现一律 POST /orgs/{login}/repos，
    GitHub 对个人账号返回 404，组织 provisioning 失败、页面显示未关联仓库。
    """
    from src.domains.mgmt.user import repo_manager

    captured = {}

    def _fake_req(method, url, token="", body=None):
        if method == "GET" and url.endswith("/user"):
            return {"login": "Misheng-chenyan"}
        captured.update({"method": method, "url": url, "body": body})
        return {"full_name": "Misheng-chenyan/test-team-grape-project"}

    monkeypatch.setattr(repo_manager, "_github_req", _fake_req)

    result = repo_manager.create_github_repo(
        "tk", "test-team-grape-project", "desc", private=False,
        owner="Misheng-chenyan", path="ignored")

    assert result["full_name"] == "Misheng-chenyan/test-team-grape-project"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/user/repos")
    assert "/orgs/" not in captured["url"]


def test_provision_uses_selected_provider_namespace_and_path(monkeypatch):
    import src.domains.mgmt.org_provision as provision
    from src.domains.mgmt.user.org_db import (
        create_org,
        get_org,
        update_org_provision_fields,
    )

    org = create_org(name="Repo Options Team", slug=SLUG, owner_id=UID)
    assert org
    update_org_provision_fields(
        org["id"],
        org_repo_provider="github",
        org_repo_owner="acme",
        org_repo_name="team-workspace",
        org_repo_path="team-path",
        org_repo_private=0,
    )

    captured = {}

    def _fake_create(body, user):
        captured["body"] = body
        return {
            "repo": {"full_name": "acme/team-path"},
            "full_name": "acme/team-path",
            "owner": "acme",
            "name": "team-path",
            "provider": "github",
            "hosted": False,
        }

    def _fake_host(body, user, request=None, provider=""):
        from src.domains.mgmt.user.db import add_managed_repo

        add_managed_repo(
            "github:acme/team-path", "acme", "team-path",
            webhook_id=4242, org_id=user["org_id"],
        )
        return {"message": "hosted", "webhook_id": 4242}

    monkeypatch.setattr(provision, "create_new_repo", _fake_create)
    monkeypatch.setattr(provision, "host_repo", _fake_host)

    result = provision.provision_org(org["id"])

    assert result["provision_status"] == "ready", result
    body = captured["body"]
    assert body.provider == "github"
    assert body.owner == "acme"
    assert body.name == "team-workspace"
    assert body.path == "team-path"
    assert body.private is False
    assert body.visibility == "public"
    assert get_org(org["id"])["org_repo_key"] == "github:acme/team-path"


# ===== GitHub 建仓权限预检（2026-11-19） =====

def test_probe_github_classic_token_without_repo_scope(monkeypatch):
    from src.domains.mgmt.user import repo_manager

    monkeypatch.setattr(repo_manager, "_github_token_scopes", lambda tok: "")
    ok, reason = repo_manager.probe_github_repo_create("tk", owner="me", private=True)
    assert ok is False and "repo" in reason


def test_probe_github_private_scope_ok(monkeypatch):
    from src.domains.mgmt.user import repo_manager

    monkeypatch.setattr(repo_manager, "_github_token_scopes", lambda tok: "repo, workflow")
    ok, _ = repo_manager.probe_github_repo_create("tk", owner="me", private=False)
    assert ok is True


def test_probe_github_fine_grained_403_means_no_permission(monkeypatch):
    from src.domains.mgmt.user import repo_manager

    monkeypatch.setattr(repo_manager, "_github_token_scopes", lambda tok: None)
    monkeypatch.setattr(repo_manager, "get_user_github_login", lambda tok: "me")

    def _boom(method, url, token="", body=None):
        raise RuntimeError("GitHub API POST /user/repos failed (403): Resource not accessible")

    monkeypatch.setattr(repo_manager, "_github_req", _boom)
    ok, reason = repo_manager.probe_github_repo_create("tk", owner="me", private=True)
    assert ok is False and "Repository creation" in reason


def test_probe_github_fine_grained_422_means_can_create(monkeypatch):
    from src.domains.mgmt.user import repo_manager

    monkeypatch.setattr(repo_manager, "_github_token_scopes", lambda tok: None)
    monkeypatch.setattr(repo_manager, "get_user_github_login", lambda tok: "me")
    monkeypatch.setattr(
        repo_manager, "_github_req",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("failed (422): name is invalid")),
    )
    ok, _ = repo_manager.probe_github_repo_create("tk", owner="me", private=True)
    assert ok is True


def test_create_org_rejects_github_without_create_permission(monkeypatch):
    """无建仓权限时创建组织直接 400，不落组织、不再异步失败。"""
    from fastapi import HTTPException

    import src.domains.mgmt.routes.orgs as orgs_route
    from src.domains.mgmt.user import repo_manager

    monkeypatch.setattr(orgs_route, "get_user_token_for", lambda *a, **kw: "tk")
    monkeypatch.setattr(
        repo_manager, "probe_github_repo_create",
        lambda *a, **kw: (False, "当前 GitHub 凭据没有创建仓库权限"),
    )

    body = orgs_route.CreateOrgBody(
        name="preflight-team", repo_provider="github",
        repo_owner="Misheng-chenyan", repo_name="preflight-team",
    )
    with pytest.raises(HTTPException) as ei:
        orgs_route._normalize_repo_create_options(body, {"id": UID, "username": USERNAME})
    assert ei.value.status_code == 400
    assert ei.value.detail.get("error_code") == "VCS_REPO_CREATE_FORBIDDEN"
