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
        lambda: {"id": UID, "username": "git_tester", "auth_type": "session_token",
                 "agent_type": "vod", "user_id": 0, "session_key": "s1",
                 "repo_key": f"gitcode:{OWNER}/{REPO}"}
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)
    monkeypatch.setattr("src.user.db.get_github_token", lambda *a, **k: BOT)


def _override_user(repo_key: str):
    """设置会话 token 用户（依赖覆盖，auth_type=session_token）。"""
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": repo_key}


@pytest.fixture
def gitcode_calls(monkeypatch):
    """Mock repo_manager._req：记录 (method, url, token, body)，返回 echo。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "token": token, "body": body})
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    return recorded


@pytest.fixture
def github_calls(monkeypatch):
    """Mock repo_manager._github_req（provider=github 分流）；记录调用。"""
    recorded = []

    def _fake_gh_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "token": token, "body": body})
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.user.repo_manager._github_req", _fake_gh_req)
    return recorded


def _get(client, path, **kw):
    return client.get(path, **kw)


def test_provider_header_and_query(gitcode_calls, _client):
    """provider 由 token 的 repo_key 前缀派生（gitcode）；不再接受调用方 header/query 覆盖。"""
    client = _client
    # 缺省 user repo_key = gitcode:o/hello → 走 GitCode
    assert _get(client, f"/api/git-repo/{OWNER}/{REPO}/branches").status_code == 200
    assert "/branches" in gitcode_calls[-1]["url"]
    assert gitcode_calls[-1]["token"] == BOT
    assert "gitcode.com" in gitcode_calls[-1]["url"]
    # 调用方误传 provider 参数/头 → 忽略，仍以 token 派生为准
    assert _get(client, f"/api/git-repo/{OWNER}/{REPO}/branches",
                headers={"X-Git-Provider": "github"}).status_code == 200
    assert "gitcode.com" in gitcode_calls[-1]["url"]
    assert _get(client, f"/api/git-repo/{OWNER}/{REPO}/branches?provider=github").status_code == 200
    assert "gitcode.com" in gitcode_calls[-1]["url"]


def test_provider_derived_from_token_gitcode(_client, gitcode_calls):
    _override_user(f"gitcode:{OWNER}/{REPO}")
    r = _get(_client, f"/api/git-repo/{OWNER}/{REPO}/branches")
    assert r.status_code == 200
    assert "gitcode.com" in gitcode_calls[-1]["url"]


def test_provider_derived_from_token_github(_client, github_calls):
    _override_user(f"github:{OWNER}/{REPO}")
    r = _get(_client, f"/api/git-repo/{OWNER}/{REPO}/branches")
    assert r.status_code == 200
    assert "api.github.com" in github_calls[-1]["url"]
    assert github_calls[-1]["token"] == BOT


def test_provider_param_ignored_when_token_authoritative(_client, github_calls):
    """token=github 时调用方误传 X-Git-Provider: gitcode → 仍走 GitHub（以 token 为准）。"""
    _override_user(f"github:{OWNER}/{REPO}")
    r = _get(_client, f"/api/git-repo/{OWNER}/{REPO}/branches",
             headers={"X-Git-Provider": "gitcode"})
    assert r.status_code == 200
    _u = github_calls[-1]["url"]
    assert "api.github.com" in _u and "gitcode.com" not in _u


def test_unknown_provider_rejected(_client):
    """token 绑定未接入 provider（gitlab）→ 403 防御。"""
    _override_user(f"gitlab:{OWNER}/{REPO}")
    r = _get(_client, f"/api/git-repo/{OWNER}/{REPO}/branches")
    assert r.status_code == 403
    assert "未接入" in (r.json().get("detail", {}).get("message", ""))


def test_repo_info_workspace(_client, gitcode_calls):
    client = _client
    assert _get(client, f"/api/git-repo/{OWNER}/{REPO}").status_code == 200
    assert gitcode_calls[-1]["method"] == "GET" and gitcode_calls[-1]["url"].endswith(f"/repos/{OWNER}/{REPO}")
    ws = _get(client, f"/api/repos/{OWNER}/{REPO}/workspace").json()
    assert ws["clone_url"] == f"https://gitcode.com/{OWNER}/{REPO}.git"
    assert "workspace" in ws


def test_repos_credential_provider_from_token(_client):
    """TC-4: /api/repos git-credential/workspace 的 provider 同样由 token 派生（github）。"""
    _override_user(f"github:{OWNER}/{REPO}")
    cred = _client.get(f"/api/repos/{OWNER}/{REPO}/git-credential").json()
    assert cred["provider"] == "github"
    assert cred["clone_url"].startswith("https://github.com/")
    ws = _client.get(f"/api/repos/{OWNER}/{REPO}/workspace").json()
    assert ws["clone_url"].startswith("https://github.com/")


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
    base = f"/api/git-repo/{OWNER}/{REPO}"

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

    # Issue #: issues update 支持修改正文（body）——透传到平台，空 body 不进入 payload
    r = client.patch(f"{base}/issues/7", json={"body": "新正文内容"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "PATCH"
    assert last["body"].get("body") == "新正文内容"
    assert "state" not in last["body"]  # 未传字段不出现（空值过滤）

    r = client.patch(f"{base}/issues/7", json={"state": "open"})  # 空 body → 不污染 payload
    last = gitcode_calls[-1]
    assert "body" not in last["body"]

    assert _get(client, f"{base}/issues/7/comments").status_code == 200
    assert gitcode_calls[-1]["url"].endswith("/issues/7/comments")

    r = client.post(f"{base}/issues/7/comments", json={"body": "LGTM"})
    assert r.status_code == 200
    assert gitcode_calls[-1]["method"] == "POST" and gitcode_calls[-1]["body"] == {"body": "LGTM"}


def test_pulls_mapping(gitcode_calls, _client):
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"

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
    base = f"/api/git-repo/{OWNER}/{REPO}"

    # 根路径（path 空）→ 请求 /contents/（目录列表语义，GitCode 返回数组）
    assert _get(client, f"{base}/contents?ref=main").status_code == 200
    last = gitcode_calls[-1]
    assert "/contents/" in last["url"] and "ref=main" in last["url"]

    # 目录路径 → /contents/configs（目录列表）
    assert _get(client, f"{base}/contents/configs?ref=main").status_code == 200
    last = gitcode_calls[-1]
    assert "/contents/configs" in last["url"] and "ref=main" in last["url"]

    # 文件路径 → /contents/CODEOWNERS（base64 单文件，不回归）
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
    r = client.get(f"/api/git-repo/{OWNER}/{REPO}/branches")
    assert r.status_code == 502


# ===================== _repo_token 按会话身份取平台 token =====================

def _repo_token_call(user):
    import src.api.git_repo_api as m
    return m._repo_token(OWNER, REPO, user)


def test_repo_token_chat_session_uses_user_token(monkeypatch):
    import src.api.git_repo_api as m
    calls = {"user": 0, "bot": 0}
    monkeypatch.setattr("src.user.db.get_gitcode_token",
                        lambda *a, **k: calls.__setitem__("bot", calls["bot"] + 1) or BOT)
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh",
                        lambda *a, **k: calls.__setitem__("user", calls["user"] + 1) or "USRTOK")
    user = {"auth_type": "session_token", "agent_type": "chat", "user_id": 7, "id": 7,
            "session_key": "t_x", "repo_key": f"gitcode:{OWNER}/{REPO}"}
    tok = _repo_token_call(user)
    assert tok == "USRTOK"
    assert calls["user"] >= 1 and calls["bot"] == 0


def test_repo_token_devops_session_uses_bot_token(monkeypatch):
    calls = {"user": 0, "bot": 0}
    monkeypatch.setattr("src.user.db.get_gitcode_token",
                        lambda *a, **k: calls.__setitem__("bot", calls["bot"] + 1) or BOT)
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh",
                        lambda *a, **k: calls.__setitem__("user", calls["user"] + 1) or "USRTOK")
    user = {"auth_type": "session_token", "agent_type": "vod", "user_id": 0,
            "session_key": "t_y", "repo_key": f"gitcode:{OWNER}/{REPO}"}
    assert _repo_token_call(user) == BOT
    assert calls["bot"] >= 1 and calls["user"] == 0


def test_repo_token_api_key_not_applicable():
    """评审：用户级 API Key 不适用于 git-repo 接口 → 403。"""
    import src.api.git_repo_api as m
    import pytest as _p
    from fastapi import HTTPException
    with _p.raises(HTTPException) as ei:
        m._repo_token(OWNER, REPO, {"auth_type": "api_key", "id": 9, "username": "someone"})
    assert ei.value.status_code == 403
    with _p.raises(HTTPException) as ei2:
        m._repo_token(OWNER, REPO, {"auth_type": "jwt", "id": 9, "username": "someone"})
    assert ei2.value.status_code == 403


def test_repo_token_all_missing_502(monkeypatch):
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "")
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh", lambda *a, **k: "")
    import src.api.git_repo_api as m
    import pytest as _p
    from fastapi import HTTPException
    with _p.raises(HTTPException) as ei:
        m._repo_token(OWNER, REPO, {"auth_type": "session_token", "agent_type": "vod", "user_id": 0})
    assert ei.value.status_code == 502


def test_unauthenticated_401():
    app.dependency_overrides.clear()
    client = TestClient(app)
    assert client.get(f"/api/git-repo/{OWNER}/{REPO}/branches").status_code == 401


def test_tree_single_level(gitcode_calls, _client):
    """选项 B：单层树透传 git/trees/{ref}。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"
    r = client.get(f"{base}/tree?ref=main")
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert "/git/trees/main" in last["url"] and last["method"] == "GET"


def test_tree_ref_empty_resolves_default(gitcode_calls, _client):
    """选项 B：ref 空时解析默认分支（branches[0]）再取树。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"
    # gitcode_calls 的 mock _req 对 branches 返回 {"ok": True}（非 list）→ _default_ref 落到 "main" 兜底
    r = client.get(f"{base}/tree")
    assert r.status_code == 200
    reqs = [c["url"] for c in gitcode_calls]
    assert any("/branches" in u for u in reqs) or any("/git/trees/main" in u for u in reqs)


def test_tree_recursive_gitcode_rejected(_client):
    """选项 B：GitCode 平台 recursive → 明确 400 拒绝，不产出破碎树。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"
    r = client.get(f"{base}/tree?ref=main&depth=recursive")
    assert r.status_code == 400
    assert "暂不支持递归" in r.json()["detail"]["message"]


# ===================== Issue Close（gitrepo_issues_close）=====================

def test_close_issue_gitcode_ok(_client):
    """POST /issues/{number}/close：gitcode provider → VcsApiClient.close_issue 关闭，返回 issue。"""
    client = _client
    with patch("src.vcs.api_client.VcsApiClient") as MC:
        inst = MC.return_value
        inst.close_issue.return_value = {"number": 5, "state": "closed", "title": "t"}
        r = client.post(f"/api/git-repo/{OWNER}/{REPO}/issues/5/close")
    assert r.status_code == 200
    assert r.json()["state"] == "closed"
    MC.assert_called_once()
    args = MC.call_args[0]
    assert args[0] == "gitcode" and args[2:] == (OWNER, REPO)
    inst.close_issue.assert_called_once_with(5)


def test_close_issue_github_provider(_client):
    """repo_key=github: → provider=github → VcsApiClient(github, ...)"""
    client = _client
    _override_user(f"github:{OWNER}/{REPO}")
    with patch("src.vcs.api_client.VcsApiClient") as MC:
        inst = MC.return_value
        inst.close_issue.return_value = {"state": "closed", "number": 7}
        r = client.post(f"/api/git-repo/{OWNER}/{REPO}/issues/7/close")
    assert r.status_code == 200
    assert MC.call_args[0][0] == "github"


def test_close_issue_error_502(_client):
    """关闭失败（VcsApiClient 返回 error）→ 502。"""
    client = _client
    with patch("src.vcs.api_client.VcsApiClient") as MC:
        inst = MC.return_value
        inst.close_issue.return_value = {"error": "HTTP 400", "detail": "bad"}
        r = client.post(f"/api/git-repo/{OWNER}/{REPO}/issues/5/close")
    assert r.status_code == 502
    assert "关闭 Issue 失败" in r.json()["detail"]["message"]


def test_close_issue_requires_session_token(_client):
    """非会话 token（用户 API Key/JWT）→ 403（_repo_token 只服务 session token）。"""
    from src.api.deps import get_current_user_or_api_key
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "api_key",
        "agent_type": "vod", "repo_key": f"gitcode:{OWNER}/{REPO}"}
    client = TestClient(app)
    r = client.post(f"/api/git-repo/{OWNER}/{REPO}/issues/5/close")
    assert r.status_code == 403