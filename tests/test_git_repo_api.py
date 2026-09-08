"""Issue-45 M3：Git 仓库统一接口测试.

覆盖：
- provider 判定（头/query/缺省/未知）
- 白名单端点 URL/方法/body 映射（issues/pulls/comments/labels/merge/branches/contents/milestones/commits）
- git-credential / workspace
- 无 token → 502；未认证 → 401
"""
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key

UID = 98701
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_123"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = \
        lambda: {"id": UID, "username": "git_tester"}
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)


@pytest.fixture
def gitcode_calls(monkeypatch):
    """Mock repo_manager._req：记录 (method, url, token, body)，返回 echo。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "token": token, "body": body})
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    return recorded


def _get(client, path, **kw):
    return client.get(path, **kw)


def test_provider_header_and_query(gitcode_calls, _client):
    client = _client
    assert _get(client, f"/api/repos/{OWNER}/{REPO}/branches").status_code == 200
    assert "/branches" in gitcode_calls[-1]["url"]
    assert gitcode_calls[-1]["token"] == BOT

    assert _get(client, f"/api/repos/{OWNER}/{REPO}/branches",
                headers={"X-Git-Provider": "gitcode"}).status_code == 200
    assert _get(client, f"/api/repos/{OWNER}/{REPO}/branches?provider=gitcode").status_code == 200


def test_unknown_provider_rejected(_client):
    client = _client
    assert _get(client, f"/api/repos/{OWNER}/{REPO}/branches",
                headers={"X-Git-Provider": "github"}).status_code == 403
    assert _get(client, f"/api/repos/{OWNER}/{REPO}/branches?provider=gitlab").status_code == 403


def test_repo_info_workspace(_client, gitcode_calls):
    client = _client
    assert _get(client, f"/api/repos/{OWNER}/{REPO}").status_code == 200
    assert gitcode_calls[-1]["method"] == "GET" and gitcode_calls[-1]["url"].endswith(f"/repos/{OWNER}/{REPO}")
    ws = _get(client, f"/api/repos/{OWNER}/{REPO}/workspace").json()
    assert ws["clone_url"] == f"https://gitcode.com/{OWNER}/{REPO}.git"
    assert "workspace" in ws


def test_git_credential(_client):
    client = _client
    r = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
    assert r.status_code == 200
    cred = r.json()
    assert cred["provider"] == "gitcode"
    assert cred["password"] == BOT
    assert cred["username"] == "x-access-token"
    assert cred["clone_url"] == f"https://gitcode.com/{OWNER}/{REPO}.git"
    assert cred["expires_in"] > 0


def test_issues_crud_mapping(gitcode_calls, _client):
    client = _client
    base = f"/api/repos/{OWNER}/{REPO}"

    assert _get(client, f"{base}/issues?state=all&per_page=10").status_code == 200
    last = gitcode_calls[-1]
    assert ("issues" in last["url"]) and ("state=all" in last["url"])

    r = client.post(f"{base}/issues", json={"title": "t", "body": "b", "assignee": "x"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "POST" and last["body"]["title"] == "t" and last["body"]["assignee"] == "x"

    assert _get(client, f"{base}/issues/7").status_code == 200
    assert gitcode_calls[-1]["url"].endswith("/issues/7")

    r = client.patch(f"{base}/issues/7", json={"state": "close", "labels": "bug"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "PATCH" and last["body"]["state"] == "close" and last["body"]["labels"] == "bug"

    assert _get(client, f"{base}/issues/7/comments").status_code == 200
    assert gitcode_calls[-1]["url"].endswith("/issues/7/comments")

    r = client.post(f"{base}/issues/7/comments", json={"body": "LGTM"})
    assert r.status_code == 200
    assert gitcode_calls[-1]["method"] == "POST" and gitcode_calls[-1]["body"] == {"body": "LGTM"}


def test_pulls_mapping(gitcode_calls, _client):
    client = _client
    base = f"/api/repos/{OWNER}/{REPO}"

    r = client.post(f"{base}/pulls",
                    json={"title": "feat", "head": "feat/x", "base": "main", "close_related_issue": True})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "POST" and last["body"]["head"] == "feat/x" and last["body"]["close_related_issue"] is True

    assert _get(client, f"{base}/pulls/9").status_code == 200
    assert gitcode_calls[-1]["url"].endswith("/pulls/9")

    r = client.put(f"{base}/pulls/9/merge")
    assert r.status_code == 200
    assert gitcode_calls[-1]["method"] == "PUT" and gitcode_calls[-1]["url"].endswith("/pulls/9/merge")

    assert _get(client, f"{base}/pulls/9/files").status_code == 200
    assert gitcode_calls[-1]["url"].endswith("/pulls/9/files")


def test_contents_branches_milestones_labels(gitcode_calls, _client):
    client = _client
    base = f"/api/repos/{OWNER}/{REPO}"

    assert _get(client, f"{base}/contents/CODEOWNERS?ref=codeowner").status_code == 200
    last = gitcode_calls[-1]
    assert "/contents/CODEOWNERS" in last["url"] and "ref=codeowner" in last["url"]

    assert _get(client, f"{base}/commits?per_page=5").status_code == 200
    assert "/commits" in gitcode_calls[-1]["url"] and "per_page=5" in gitcode_calls[-1]["url"]

    assert _get(client, f"{base}/milestones?state=all").status_code == 200
    assert "/milestones" in gitcode_calls[-1]["url"] and "state=all" in gitcode_calls[-1]["url"]

    r = client.post(f"{base}/labels", json={"name": "bug", "color": "#d73a4a"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "POST" and last["body"]["name"] == "bug" and last["body"]["color"] == "#d73a4a"


def test_no_token_502(monkeypatch, _client):
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "")
    client = _client
    r = client.get(f"/api/repos/{OWNER}/{REPO}/branches")
    assert r.status_code == 502


def test_unauthenticated_401():
    app.dependency_overrides.clear()
    client = TestClient(app)
    assert client.get(f"/api/repos/{OWNER}/{REPO}/branches").status_code == 401