"""D102-O：git-repo 传输层 issue/PR 编号字符串 ident 契约（原生保形）。

背景（实弹 Issue #IKTREU，gitee 仓库）：gitee Issue 编号为字符串 ident
（"IKTREU" 形态），而 git_repo_api 全部 issue/pulls 路由路径参数声明为
`number: int`——FastAPI 路径校验阶段即拒（422），请求到不了业务代码，
A2A 作业评论落地/标签更新/关单在字符串 ident 平台整族不可用。

D102-G 只修了流水线进程内 ident 链路（isdigit 门槛删除），HTTP 契约面
是另一层。本批把 number 改 str（原生保形，客户端层 _native_issue_number
D41/S61 已就绪），并用例锁定：
- 字符串 ident 平台（gitee）：评论/关单/查详情/PULLS 均不再 422，URL 原生保形
- 纯数字平台（gitcode）：行为与改前一致（回归锁）
- milestone/comment_id 的 int 契约不动（全平台数字，非本批放开面）
"""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key

UID = 98801
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_123"
STR_IDENT = "IKTREU"


def _override_user(repo_key: str, agent_type: str = "vod"):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": agent_type, "user_id": 0, "session_key": "s1",
        "repo_key": repo_key}


@pytest.fixture
def gitee_client(monkeypatch):
    """provider=gitee（字符串 ident 平台代表）：token 走 bot，request 记录调用。"""
    _override_user(f"gitee:{OWNER}/{REPO}")
    monkeypatch.setattr("src.domains.mgmt.user.db.get_repo_bot_token",
                        lambda repo_key=None, **k: BOT)
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "token": token, "body": body})
        # gitcode/gitee PATCH issues 必填 title 回取（#640）——回取给 title 防 502
        if method == "GET" and "/issues/" in url and "/comments" not in url:
            return {"ok": True, "title": "t", "number": url.rstrip("/").rsplit("/", 1)[-1]}
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.domains.runtime.vcs.providers.gitee.request", _fake_req)
    yield TestClient(app), recorded
    app.dependency_overrides.clear()


@pytest.fixture
def gitcode_client(monkeypatch):
    """provider=gitcode（纯数字平台代表）：回归锁——行为与改前一致。"""
    _override_user(f"gitcode:{OWNER}/{REPO}")
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token", lambda *a, **k: BOT)
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "token": token, "body": body})
        if method == "GET" and "/issues/" in url and "/comments" not in url:
            return {"ok": True, "title": "t"}
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    yield TestClient(app), recorded
    app.dependency_overrides.clear()


def test_gitee_string_ident_comment_not_422(gitee_client):
    """实弹场景（Issue #IKTREU）：字符串 ident 平台评论落地不得被路径校验 422 拦死。"""
    client, calls = gitee_client
    r = client.post(f"/api/git-repo/{OWNER}/{REPO}/issues/{STR_IDENT}/comments",
                    json={"body": "分析结论：已实现，建议关闭"})
    assert r.status_code == 200, r.text
    assert f"/issues/{STR_IDENT}/comments" in calls[-1]["url"]


def test_gitee_string_ident_get_issue(gitee_client):
    client, calls = gitee_client
    r = client.get(f"/api/git-repo/{OWNER}/{REPO}/issues/{STR_IDENT}")
    assert r.status_code == 200, r.text
    assert f"/issues/{STR_IDENT}" in calls[-1]["url"]


def test_gitee_string_ident_close_issue(gitee_client, monkeypatch):
    """A2A 收官链：字符串 ident 关单（close 走 VcsApiClient，mock 防真连上游）。"""
    client, calls = gitee_client
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as MC:
        MC.return_value.close_issue.return_value = {"state": "closed",
                                                    "number": STR_IDENT}
        r = client.post(f"/api/git-repo/{OWNER}/{REPO}/issues/{STR_IDENT}/close")
    assert r.status_code == 200, r.text
    MC.return_value.close_issue.assert_called_once_with(STR_IDENT)


def test_gitee_string_ident_labels_update(gitee_client):
    """A2A 链标签更新（PATCH issues）。"""
    client, calls = gitee_client
    r = client.patch(f"/api/git-repo/{OWNER}/{REPO}/issues/{STR_IDENT}",
                     json={"labels": "resolved"})
    assert r.status_code == 200, r.text
    assert f"/issues/{STR_IDENT}" in calls[-1]["url"]


def test_gitee_string_ident_pull_get(gitee_client):
    """pulls 同契约：字符串 ident 不 422（PR 编号同源）。"""
    client, calls = gitee_client
    r = client.get(f"/api/git-repo/{OWNER}/{REPO}/pulls/{STR_IDENT}")
    assert r.status_code == 200, r.text
    assert f"/pulls/{STR_IDENT}" in calls[-1]["url"]


def test_gitcode_numeric_ident_regression(gitcode_client):
    """回归锁：纯数字平台（gitcode #123）行为与改前一致。"""
    client, calls = gitcode_client
    r = client.post(f"/api/git-repo/{OWNER}/{REPO}/issues/123/comments",
                    json={"body": "numeric"})
    assert r.status_code == 200, r.text
    assert "/issues/123/comments" in calls[-1]["url"]
    r2 = client.get(f"/api/git-repo/{OWNER}/{REPO}/issues/123")
    assert r2.status_code == 200
