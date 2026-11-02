"""Issue-45 M3：Git 仓库统一接口测试.

覆盖：
- provider 判定（头/query/缺省/未知）
- 白名单端点 URL/方法/body 映射（issues/pulls/comments/labels/merge/branches/contents/milestones/commits）
- git-credential / workspace
- 无 token → 502；未认证 → 401
"""
import json
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.user.repo_manager import UpstreamAPIError

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
        # "title"：gitcode issues update PATCH 必填 title 自动回取（#640），回取为空会 502
        # "/commits?sha="：tree ref 存在性校验（#734）→ 返回带 tree sha 的单条 commit
        if "/commits" in url and "sha=" in url:
            return [{"sha": "c0ffee", "commit": {"tree": {"sha": "tree0f9e8d"}, "message": "m"}}]
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}, "title": "t"}

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
                    json={"title": "feat", "head": "feat/x", "base": "main", "auto_close_issues": True})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "POST" and last["body"]["head"] == "feat/x" and last["body"]["close_related_issue"] is True

    assert _get(client, f"{base}/pulls/9").status_code == 200
    assert gitcode_calls[-1]["url"].endswith("/pulls/9")

    r = client.put(f"{base}/pulls/9/merge")
    assert r.status_code == 200
    assert gitcode_calls[-1]["method"] == "PUT" and gitcode_calls[-1]["url"].endswith("/pulls/9/merge")

    assert _get(client, f"{base}/pulls/9/files").status_code == 200
    assert gitcode_calls[-1]["url"].split("?")[0].endswith("/pulls/9/files")
    # Issue #585：默认透传分页参数（page=1, per_page=100）
    assert "page=1" in gitcode_calls[-1]["url"] and "per_page=100" in gitcode_calls[-1]["url"]


def test_pulls_files_pagination_passthrough(gitcode_calls, _client):
    """Issue #585 Bug 2：pulls files 透传 page/per_page 分页参数（默认 30 截断修复）。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"

    r = _get(client, f"{base}/pulls/9/files", params={"page": 2, "per_page": 50})
    assert r.status_code == 200
    url = gitcode_calls[-1]["url"]
    assert url.split("?")[0].endswith("/pulls/9/files")
    assert "page=2" in url and "per_page=50" in url

    # 非法分页参数 → 422（不触达上游）
    r = _get(client, f"{base}/pulls/9/files", params={"per_page": 101})
    assert r.status_code == 422
    r = _get(client, f"{base}/pulls/9/files", params={"page": 0})
    assert r.status_code == 422


def test_create_pull_comment_github_uses_issue_comments_endpoint(_client, github_calls):
    """Issue #585 Bug 1：GitHub 通道 PR 评论改走 /issues/{n}/comments（仅需 body），
    不再误用 inline review comment 端点 /pulls/{n}/comments（422 Validation Failed）。"""
    _override_user(f"github:{OWNER}/{REPO}")
    r = _client.post(f"/api/git-repo/{OWNER}/{REPO}/pulls/9/comments",
                     json={"body": "LGTM"})
    assert r.status_code == 200
    last = github_calls[-1]
    assert last["method"] == "POST"
    assert last["url"].split("?")[0].endswith(f"/repos/{OWNER}/{REPO}/issues/9/comments")
    assert "/pulls/9/comments" not in last["url"]
    assert "<!-- grape-auto-comment -->" in last["body"]["body"]


def test_create_pull_comment_gitcode_keeps_pulls_endpoint(gitcode_calls, _client):
    """Issue #585 Bug 1：GitCode 通道保持 /pulls/{n}/comments（行为不变，向后兼容）。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"
    r = client.post(f"{base}/pulls/9/comments", json={"body": "LGTM"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "POST"
    assert last["url"].split("?")[0].endswith("/pulls/9/comments")
    assert last["body"]["body"].endswith("\n<!-- grape-auto-comment -->")


def test_pulls_comments_update_delete(gitcode_calls, _client):
    """Issue #563：PR 评论 update/delete 透传上游 /pulls/comments/{id}（comment id 全局唯一，不含 PR number）。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"

    r = client.patch(f"{base}/pulls/9/comments/123", json={"body": "corrected"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "PATCH"
    assert last["url"].endswith("/pulls/comments/123")
    assert last["body"] == {"body": "corrected"}

    r = client.delete(f"{base}/pulls/9/comments/123")
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "DELETE"
    assert last["url"].endswith("/pulls/comments/123")
    assert last["body"] is None


def test_pulls_comments_update_hex_id_compat(monkeypatch, _client):
    """Issue #623：update comment_id 兼容十六进制 id——单评论查询取数字 note_id 后透传 PATCH。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        if method == "GET" and "/pulls/comments/" in url:
            return {"id": 421, "body": "old"}
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)

    r = client.patch(f"{base}/pulls/9/comments/0df1a2", json={"body": "corrected"})
    assert r.status_code == 200
    assert recorded[0]["method"] == "GET"
    assert recorded[0]["url"].endswith("/pulls/comments/0df1a2")
    last = recorded[-1]
    assert last["method"] == "PATCH"
    assert last["url"].endswith("/pulls/comments/421")
    assert last["body"] == {"body": "corrected"}


def test_pulls_comments_update_hex_id_fallback_list(monkeypatch, _client):
    """Issue #623：单评论查询无 note_id 时回退 PR 评论列表按 discussion_id 匹配。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        if method == "GET" and "/pulls/comments/" in url:
            return {"ok": True}
        if method == "GET" and url.endswith("/pulls/9/comments"):
            return [{"id": 77, "discussion_id": "0df1a2", "body": "old"},
                    {"id": 78, "discussion_id": "00ff00", "body": "other"}]
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)

    r = client.patch(f"{base}/pulls/9/comments/0df1a2", json={"body": "corrected"})
    assert r.status_code == 200
    last = recorded[-1]
    assert last["method"] == "PATCH"
    assert last["url"].endswith("/pulls/comments/77")


def test_pulls_comments_update_invalid_id_422(gitcode_calls, _client):
    """Issue #623：非法 comment_id（非数字/非十六进制）返回 422 且不触达上游。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"
    r = client.patch(f"{base}/pulls/9/comments/zz-9x!", json={"body": "x"})
    assert r.status_code == 422
    assert "note_id" in r.json()["detail"]["message"]
    assert gitcode_calls == []


def test_pulls_comments_update_hex_id_unresolvable_422(monkeypatch, _client):
    """Issue #623：十六进制 id 解析不到 note_id（上游查无此评论）返回明确 422。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"

    def _fake_req(method, url, token="", body=None):
        if method == "GET" and "/pulls/comments/" in url:
            return {"ok": True}
        if method == "GET" and url.endswith("/pulls/9/comments"):
            return []
        return {"ok": True}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)

    r = client.patch(f"{base}/pulls/9/comments/abcdef", json={"body": "x"})
    assert r.status_code == 422
    assert "note_id" in r.json()["detail"]["message"]


def test_pulls_comments_update_numeric_id_passthrough(gitcode_calls, _client):
    """Issue #623：数字 note_id 直传行为保持不变（向后兼容）。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"
    r = client.patch(f"{base}/pulls/9/comments/123", json={"body": "same"})
    assert r.status_code == 200
    assert len(gitcode_calls) == 1
    last = gitcode_calls[-1]
    assert last["method"] == "PATCH"
    assert last["url"].endswith("/pulls/comments/123")


def test_pulls_comments_update_delete_contract_operations():
    """Issue #563：新端点进 OpenAPI 契约且标注 session_only（CLI refresh 后自动获得命令）。"""
    from src.api.openapi import custom_openapi
    schema = custom_openapi(app)
    ops = {(p, m) for p, methods in schema["paths"].items() for m in methods}
    _cp = "/api/git-repo/{owner}/{repo}/pulls/{number}/comments/{comment_id}"
    assert (_cp, "patch") in ops
    assert (_cp, "delete") in ops
    op = schema["paths"][_cp]["patch"]
    assert op["operationId"] == "gitrepo_pulls_comments_update"
    assert op.get("x-token-scope") == "session_only"


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
    assert last["method"] == "POST" and last["body"]["name"] == "bug" and last["body"]["color"] == "d73a4a"


# ===================== Issue #714: 特殊字符路径逐段 percent-encode =====================

def test_contents_special_chars_plus_encoded(gitcode_calls, _client):
    """Issue #714：含 + 路径 → 转发 URL 中 + 编码为 %2B（不再被对端解析为空格）。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    assert _get(_client, f"{base}/contents/c++/README.md").status_code == 200
    url = gitcode_calls[-1]["url"]
    assert "/contents/c%2B%2B/README.md" in url
    assert "+" not in url.split("?")[0]


def test_contents_special_chars_chinese_and_space_encoded(gitcode_calls, _client):
    """Issue #714：中文 + 空格路径 → UTF-8 percent-encode、空格→%20。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    assert _get(_client, f"{base}/contents/docs/文件 名.md").status_code == 200
    url = gitcode_calls[-1]["url"]
    assert "/contents/docs/%E6%96%87%E4%BB%B6%20%E5%90%8D.md" in url


def test_contents_special_chars_ascii_unaffected(gitcode_calls, _client):
    """Issue #714 回归：ASCII 路径（含 -/_/.）编码后不变。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    assert _get(_client, f"{base}/contents/configs/sub-dir_v1.0.py").status_code == 200
    assert "/contents/configs/sub-dir_v1.0.py" in gitcode_calls[-1]["url"]


def test_contents_special_chars_404_passthrough(upstream_error, _client):
    """Issue #714：不存在的特殊字符路径 → 上游 404 透传为明确 404（非空内容/空数组）。"""
    upstream_error(404, '{"message":"Not Found"}')
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/contents/不存在的+文件.md")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == 404


def test_vcs_url_encodes_owner_repo_and_subpath_segments():
    """Issue #714 单元级：owner/repo/subpath 逐段 quote，/ 分隔语义保留。"""
    from src.api.git_repo_api import _vcs_url
    url = _vcs_url("o w", "re+po", "/contents/c++/文件 名.md", {"ref": "main"}, "gitcode")
    assert "/repos/o%20w/re%2Bpo/contents/c%2B%2B/%E6%96%87%E4%BB%B6%20%E5%90%8D.md" in url
    assert url.endswith("?ref=main")


def test_labels_color_normalization(gitcode_calls, github_calls, _client):
    """Issue #580：labels create 对 color 做 lstrip('#') 归一化——GitHub/GitCode 双通道 × 带/不带 # 双输入。

    GitHub REST API labels 端点要求 color 为不带 # 的 6 位 hex，带 # 返回 422 Validation Failed；
    GitCode 通道对两种格式均宽容。归一化放在服务端透传层（单点修复，所有客户端受益）。
    """
    client = _client

    # GitCode 通道（默认 token 派生 provider=gitcode）
    for color in ("#e11d21", "e11d21"):
        assert client.post(f"/api/git-repo/{OWNER}/{REPO}/labels", json={"name": "P1", "color": color}).status_code == 200
        assert gitcode_calls[-1]["body"] == {"name": "P1", "color": "e11d21"}

    # GitHub 通道（repo_key 前缀 github 派生 provider）
    _override_user(f"github:{OWNER}/{REPO}")
    for color in ("#e11d21", "e11d21"):
        assert client.post(f"/api/git-repo/{OWNER}/{REPO}/labels", json={"name": "P1", "color": color}).status_code == 200
        assert github_calls[-1]["method"] == "POST"
        assert "api.github.com" in github_calls[-1]["url"]
        assert github_calls[-1]["body"] == {"name": "P1", "color": "e11d21"}

    # 不传 color → 默认值 d73a4a（不带 # 前缀）
    assert client.post(f"/api/git-repo/{OWNER}/{REPO}/labels", json={"name": "def"}).status_code == 200
    assert github_calls[-1]["body"] == {"name": "def", "color": "d73a4a"}


def test_milestones_create_update(gitcode_calls, _client):
    """Issue #632：POST/PATCH milestones 透传（provider 直调，空值字段过滤）。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"

    # create：title 必填，description/due_on 透传
    r = client.post(f"{base}/milestones", json={"title": "v1.0", "description": "first release",
                                                "due_on": "2026-12-01T00:00:00Z"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "POST" and last["url"].endswith("/milestones")
    assert last["body"] == {"title": "v1.0", "description": "first release",
                            "due_on": "2026-12-01T00:00:00Z"}

    # create 缺 title → 422（契约校验）
    assert client.post(f"{base}/milestones", json={"description": "no title"}).status_code == 422

    # update：PATCH /milestones/{number}，空值字段不透传
    r = client.patch(f"{base}/milestones/3", json={"title": "v1.1", "state": "closed"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "PATCH" and last["url"].endswith("/milestones/3")
    assert last["body"] == {"title": "v1.1", "state": "closed"}

    # update：全空 body → 空对象透传（不炸）
    r = client.patch(f"{base}/milestones/3", json={})
    assert r.status_code == 200
    assert gitcode_calls[-1]["body"] == {}


def test_no_token_502(monkeypatch, _client):
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "")
    client = _client
    r = client.get(f"/api/git-repo/{OWNER}/{REPO}/branches")
    assert r.status_code == 502


# ===================== _repo_token 按会话身份取平台 token =====================

def _repo_token_call(user):
    import src.api.git_repo_api as m
    return m._repo_token(OWNER, REPO, user, "gitcode")


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
        m._repo_token(OWNER, REPO, {"auth_type": "api_key", "id": 9, "username": "someone"}, "gitcode")
    assert ei.value.status_code == 403
    with _p.raises(HTTPException) as ei2:
        m._repo_token(OWNER, REPO, {"auth_type": "jwt", "id": 9, "username": "someone"}, "gitcode")
    assert ei2.value.status_code == 403


def test_repo_token_all_missing_502(monkeypatch):
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "")
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh", lambda *a, **k: "")
    import src.api.git_repo_api as m
    import pytest as _p
    from fastapi import HTTPException
    with _p.raises(HTTPException) as ei:
        m._repo_token(OWNER, REPO, {"auth_type": "session_token", "agent_type": "vod", "user_id": 0}, "gitcode")
    assert ei.value.status_code == 502


def test_unauthenticated_401():
    app.dependency_overrides.clear()
    client = TestClient(app)
    assert client.get(f"/api/git-repo/{OWNER}/{REPO}/branches").status_code == 401


def test_tree_single_level(gitcode_calls, _client):
    """选项 B：单层树先经 commits 校验 ref 存在性再透传 git/trees/{sha}（#734）。"""
    client = _client
    base = f"/api/git-repo/{OWNER}/{REPO}"
    r = client.get(f"{base}/tree?ref=main")
    assert r.status_code == 200
    reqs = [c["url"] for c in gitcode_calls]
    assert any("/commits?sha=main" in u for u in reqs), "ref 必须先经 commits 存在性校验"
    last = gitcode_calls[-1]
    assert "/git/trees/" in last["url"] and last["method"] == "GET"


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


class TestRepoCredentialProviderByPath:
    """Issue #466 断点一：/api/repos git-credential 的 provider 按请求路径目标仓
    repo_key 前缀（gitcode:/github:）解析，无前缀回退会话主仓。

    场景：GitHub 主仓 + GitCode 关联仓联合开发。会话 token repo_key 按主仓
    （github:...）签发；agent 换券关联仓时路径应带 gitcode: 前缀，服务端须按
    目标仓宿主解析，而非把 ghp_ token 发给 gitcode.com。
    """

    def test_cross_platform_path_prefix_gitcode(self, monkeypatch, _client):
        """主仓 github: 会话 + 请求 gitcode: 前缀目标仓 → provider=gitcode、走 GitCode 解析链。"""
        calls = {"gitcode": 0, "github": 0}
        seen = {}

        def _gc(*a, **k):
            calls["gitcode"] += 1
            seen["repo_key"] = k.get("repo_key")
            return "gc_tok"

        monkeypatch.setattr("src.user.db.get_gitcode_token", _gc)
        monkeypatch.setattr("src.user.db.get_github_token",
                            lambda *a, **k: (calls.__setitem__("github", calls["github"] + 1), "gh_tok")[1])
        _override_user(f"github:{OWNER}/{REPO}")
        r = _client.get("/api/repos/gitcode:linked/hello/git-credential")
        assert r.status_code == 200
        cred = r.json()
        assert cred["provider"] == "gitcode"
        assert cred["password"] == "gc_tok"
        assert cred["clone_url"] == "https://gitcode.com/linked/hello.git", \
            "clone_url 不得泄漏 provider 前缀"
        assert calls["gitcode"] == 1 and calls["github"] == 0, \
            "应只调 GitCode 解析链，不得触碰 GitHub token"
        assert seen.get("repo_key") == "gitcode:linked/hello", \
            "传给 token 解析的 repo_key 必须为单前缀（gitcode:linked/hello），不得拼成双前缀"

    def test_cross_platform_path_prefix_github(self, monkeypatch, _client):
        """主仓 gitcode: 会话 + 请求 github: 前缀目标仓 → provider=github（反向组合）。"""
        seen = {}
        monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "gc_tok")

        def _gh(*a, **k):
            seen["repo_key"] = k.get("repo_key")
            return "gh_tok"

        monkeypatch.setattr("src.user.db.get_github_token", _gh)
        _override_user(f"gitcode:{OWNER}/{REPO}")
        r = _client.get("/api/repos/github:linked/hello/git-credential")
        assert r.status_code == 200
        cred = r.json()
        assert cred["provider"] == "github"
        assert cred["password"] == "gh_tok"
        assert cred["clone_url"] == "https://github.com/linked/hello.git"
        assert seen.get("repo_key") == "github:linked/hello", \
            "owner 带前缀时传给 token 解析的 repo_key 必须为单前缀（github:linked/hello），" \
            "不得拼成 github:github:linked/hello 导致精确匹配 managed_repos 查不到换券 502"

    def test_no_prefix_falls_back_to_session_repo(self, monkeypatch, _client):
        """路径无前缀 → 回退会话主仓 provider（向后兼容，同平台现状不回归）。"""
        monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "gc_tok")
        monkeypatch.setattr("src.user.db.get_github_token", lambda *a, **k: "gh_tok")
        _override_user(f"github:{OWNER}/{REPO}")
        r = _client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
        assert r.status_code == 200
        cred = r.json()
        assert cred["provider"] == "github"
        assert cred["password"] == "gh_tok"

    def test_unknown_path_prefix_falls_back(self, monkeypatch, _client):
        """路径带未接入前缀（gitlab:）→ 不按前缀解析，回退会话主仓。"""
        monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "gc_tok")
        _override_user(f"gitcode:{OWNER}/{REPO}")
        r = _client.get("/api/repos/gitlab:linked/hello/git-credential")
        assert r.status_code == 200
        cred = r.json()
        assert cred["provider"] == "gitcode", "未知前缀应回退会话主仓（gitcode）"

    def test_workspace_provider_follows_path_prefix(self, monkeypatch, _client):
        """workspace 接口共用 _repo_provider：带前缀路径 clone_url 按目标宿主生成。"""
        _override_user(f"github:{OWNER}/{REPO}")
        ws = _client.get("/api/repos/gitcode:linked/hello/workspace").json()
        assert ws["clone_url"].startswith("https://gitcode.com/")
        ws2 = _client.get(f"/api/repos/{OWNER}/{REPO}/workspace").json()
        assert ws2["clone_url"].startswith("https://github.com/")

# ===================== Issue #549: 上游错误码透传（404 不再被 502 包装） =====================

@pytest.fixture
def upstream_error(monkeypatch):
    """Mock repo_manager._req：抛 UpstreamAPIError，模拟上游 GitCode >=400 响应。"""
    def _install(status_code: int, message: str):
        def _fake_req(method, url, token="", body=None):
            raise UpstreamAPIError(method, url, status_code, message)
        monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    return _install


def test_upstream_404_passthrough(upstream_error, _client):
    """contents 不存在路径（上游 404）→ HTTP 404 + 统一包络（不再 502 包装）。"""
    upstream_error(404, '{"message":"Not Found","documentation_url":"https://gitcode.com"}')
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/contents/no/such/path.py")
    assert r.status_code == 404
    envelope = r.json()["detail"]
    assert envelope["code"] == 404
    assert "Not Found" in envelope["message"]


def test_upstream_403_passthrough(upstream_error, _client):
    """上游 403 → HTTP 403 透传（真实权限语义）。"""
    upstream_error(403, '{"message":"Forbidden"}')
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/branches")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == 403


def test_upstream_405_passthrough(upstream_error, _client):
    """上游 405（如 pulls merge）→ HTTP 405 透传（#558 同根因场景）。"""
    upstream_error(405, '{"message":"Method Not Allowed"}')
    r = _client.put(f"/api/git-repo/{OWNER}/{REPO}/pulls/1/merge")
    assert r.status_code == 405
    assert r.json()["detail"]["code"] == 405


def test_network_error_still_502(monkeypatch, _client):
    """代理自身故障（连接失败 RuntimeError，非上游响应）→ 仍 502。"""
    def _fake_req(method, url, token="", body=None):
        raise RuntimeError("Cannot reach GitCode API: connection refused")
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/contents/README.md")
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == 502


# ===================== Issue #734: contents --ref 404 回归锁定 =====================

def test_contents_nonexistent_branch_ref_404(upstream_error, _client):
    """contents + 不存在分支 ref（上游 404）→ 透传 404 + 统一包络（回归锁定）。"""
    upstream_error(404, '{"message":"Not Found"}')
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/contents/README.md?ref=nosuchref")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == 404


def test_contents_root_nonexistent_ref_404(upstream_error, _client):
    """根目录 contents + 不存在 ref → 透传 404（与子路径行为一致）。"""
    upstream_error(404, '{"message":"Not Found"}')
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/contents?ref=nosuchref")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == 404


def test_contents_deep_path_404_consistent(upstream_error, _client):
    """深路径 + 不存在 ref → 404（#783 场景：深/浅路径行为一致，不再返回空）。"""
    upstream_error(404, '{"message":"Not Found"}')
    r = _client.get(
        f"/api/git-repo/{OWNER}/{REPO}/contents/.skills/issue-cli/references/runbook.md?ref=dev-dzr")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == 404
    assert "Not Found" in r.json()["detail"]["message"]


def test_upstream_error_keeps_runtime_error_compat(_client):
    """UpstreamAPIError 继承 RuntimeError（既有 except RuntimeError 调用点零改动兼容）。"""
    e = UpstreamAPIError("GET", "https://gitcode.com/api/v5/x", 404, "Not Found")
    assert isinstance(e, RuntimeError)
    assert e.status_code == 404
    # str 格式兼容 _extract_http_code 正则
    assert "(404)" in str(e)
    from src.user.repo_manager import _extract_http_code
    assert _extract_http_code(e) == 404


# ===================== Issue #684 =====================

def test_json_out_control_chars_parseable(monkeypatch, _client):
    """Issue #684 验收：上游返回含原始控制字符/孤立代理项的字符串字段，
    git-repo 读端点响应体可 json.loads 无预处理直接解析（统一序列化出口）。"""
    raw_body = "line1\nline2\x01\x1f\x7f end \ud800"
    def _fake_req(method, url, token="", body=None):
        return [{"id": 1, "title": "t", "body": raw_body, "state": "opened"}]
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/pulls")
    assert r.status_code == 200
    assert b"\x01" not in r.content  # 原始控制字符不落字节流
    parsed = json.loads(r.content)  # 无预处理直接解析
    assert parsed[0]["body"] == raw_body


def test_milestones_state_normalized(monkeypatch, _client):
    """Issue #684：milestones 返回 state 归一化 active/opened→open（与过滤合法值口径对齐）。"""
    calls = []
    def _fake_req(method, url, token="", body=None):
        calls.append({"method": method, "url": url, "token": token, "body": body})
        if method == "GET" and "/milestones" in url:
            return [{"id": 1, "title": "v1", "state": "active"},
                    {"id": 2, "title": "v2", "state": "closed"},
                    {"id": 3, "title": "v3", "state": "opened"}]
        return {"id": 9, "title": "v9", "state": "active"}
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    base = f"/api/git-repo/{OWNER}/{REPO}"
    r = _client.get(f"{base}/milestones?state=all")
    assert r.status_code == 200
    assert "state=all" in calls[-1]["url"]
    items = r.json()
    assert [i["state"] for i in items] == ["open", "closed", "open"]

    r = _client.post(f"{base}/milestones", json={"title": "v9"})
    assert r.status_code == 200
    assert r.json()["state"] == "open"

    r = _client.patch(f"{base}/milestones/3", json={"state": "closed"})
    assert r.status_code == 200
    assert r.json()["state"] == "open"  # 上游返回 active → 归一化 open


def test_milestones_state_passthrough_filter(gitcode_calls, _client):
    """过滤参数仍按上游口径 open/closed/all 原样透传（归一化仅作用于返回字段）。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    assert _client.get(f"{base}/milestones?state=closed").status_code == 200
    assert "state=closed" in gitcode_calls[-1]["url"]


def test_pulls_files_diff_normalization(monkeypatch, _client):
    """Issue #731：pulls files 出参 diff 字段归一化统一至 .patch.diff。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    upstream = [
        # GitCode 正常结构：已在 .patch.diff → 保持不变（含 patch 元信息）
        {"filename": "src/a.py", "status": "modified",
         "patch": {"diff": "@@ -1 +1 @@\n+a", "added_lines": 1, "removed_lines": 1}},
        # 平台差异返回：顶层 .diff 而无 .patch → 迁入 .patch.diff 并移除顶层字段
        {"filename": "app.py", "status": "modified", "diff": "@@ -1 +1 @@\n+b"},
        # 顶层 .diff 且 .patch 已有元信息但缺 diff → 合并保留元信息
        {"filename": "scripts/x.sh", "status": "modified",
         "diff": "@@ -1 +1 @@\n+c", "patch": {"added_lines": 1, "removed_lines": 0}},
        # 顶层 .diff 且 .patch.diff 已存在 → 以 .patch.diff 为准，顶层移除
        {"filename": "dup.py", "status": "modified",
         "diff": "@@ stale @@", "patch": {"diff": "@@ -1 +1 @@\n+d"}},
    ]

    def _fake_req(method, url, token="", body=None):
        return [dict(f) for f in upstream]

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = _get(_client, f"{base}/pulls/9/files")
    assert r.status_code == 200
    files = r.json()

    assert files[0]["patch"]["diff"] == "@@ -1 +1 @@\n+a"
    assert files[0]["patch"]["added_lines"] == 1

    assert "diff" not in files[1] and files[1]["patch"]["diff"] == "@@ -1 +1 @@\n+b"

    assert "diff" not in files[2]
    assert files[2]["patch"]["diff"] == "@@ -1 +1 @@\n+c"
    assert files[2]["patch"]["added_lines"] == 1  # 元信息保留

    assert "diff" not in files[3] and files[3]["patch"]["diff"] == "@@ -1 +1 @@\n+d"

    # 一致性：所有文件对象 diff 只在 .patch.diff
    for f in files:
        assert isinstance(f["patch"], dict) and f["patch"]["diff"]


def test_pulls_files_diff_patch_string_form(monkeypatch, _client):
    """Issue #731：旧式 string 值 .patch 统一收敛为 object 形式（diff 落 .patch.diff）。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    upstream = [{"filename": "src/a.py", "status": "modified", "patch": "@@ -1 +1 @@\n+a"}]

    monkeypatch.setattr("src.user.repo_manager._req",
                        lambda m, u, token="", body=None: [dict(f) for f in upstream])
    r = _get(_client, f"{base}/pulls/9/files")
    assert r.status_code == 200
    f = r.json()[0]
    assert isinstance(f["patch"], dict) and f["patch"]["diff"] == "@@ -1 +1 @@\n+a"


def test_pulls_files_diff_non_list_passthrough(monkeypatch, _client):
    """Issue #731：非 list 出参（如错误回显 dict）不做归一化，原样透传。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    monkeypatch.setattr("src.user.repo_manager._req",
                        lambda m, u, token="", body=None: {"ok": True})
    r = _get(_client, f"{base}/pulls/9/files")
    assert r.status_code == 200 and r.json() == {"ok": True}
