"""Issue #581：issues/pulls update labels 双通道适配测试.

覆盖：
- GitHub 通道：逗号分隔字符串 → 字符串数组（修复 422）
- GitCode 通道：逗号分隔字符串原样透传（存量行为零变更）
- 数组直传：GitHub 保持数组；GitCode join 为逗号字符串
- 空项/边界：空串、空列表、纯空白项、逗号尾随空项
- Pydantic 模型：labels 接受 str|list（CLI --json 数组不再 422）
"""
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.api.git_repo_api import IssueUpdateBody, PRUpdateBody, _normalize_labels, _provider_labels

UID = 98758
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_123"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = \
        lambda: {"id": UID, "username": "labels_tester", "auth_type": "session_token",
                 "agent_type": "vod", "user_id": 0, "session_key": "s1",
                 "repo_key": f"gitcode:{OWNER}/{REPO}"}
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)
    monkeypatch.setattr("src.user.db.get_github_token", lambda *a, **k: BOT)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _override_user(repo_key: str):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "labels_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": repo_key}


@pytest.fixture
def gitcode_calls(monkeypatch):
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "token": token, "body": body})
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    return recorded


@pytest.fixture
def github_calls(monkeypatch):
    recorded = []

    def _fake_gh_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "token": token, "body": body})
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.user.repo_manager._github_req", _fake_gh_req)
    return recorded


# ---------------- 归一化入口单元测试 ----------------

def test_normalize_labels_str_splits_and_strips():
    assert _normalize_labels("bug, P1 , triaged") == ["bug", "P1", "triaged"]


def test_normalize_labels_list_coerces_and_strips():
    assert _normalize_labels(["bug", " P1 ", ""]) == ["bug", "P1"]


def test_normalize_labels_empty_variants():
    assert _normalize_labels("") == ""
    assert _normalize_labels(None) == ""
    assert _normalize_labels([]) == ""
    assert _normalize_labels(", ,") == ""


def test_normalize_labels_drops_empty_items_between_commas():
    assert _normalize_labels("bug,,P1,") == ["bug", "P1"]


# ---------------- 通道适配单元测试 ----------------

def test_provider_labels_github_string_to_array():
    assert _provider_labels("github", "bug,P1") == ["bug", "P1"]


def test_provider_labels_github_array_passthrough():
    assert _provider_labels("github", ["bug", "P1"]) == ["bug", "P1"]


def test_provider_labels_gitcode_string_passthrough():
    assert _provider_labels("gitcode", "bug,P1") == "bug,P1"


def test_provider_labels_gitcode_list_joined():
    assert _provider_labels("gitcode", ["bug", "P1"]) == "bug,P1"


def test_provider_labels_empty_passthrough():
    assert _provider_labels("github", "") == ""
    assert _provider_labels("gitcode", []) == ""


# ---------------- API 端点测试（GitHub 通道） ----------------

def test_github_issues_update_labels_string_to_array(github_calls, _client):
    """GitHub PATCH /issues 的 labels 必须是数组——逗号分隔字符串 split 后落地（422 修复）。"""
    _override_user(f"github:{OWNER}/{REPO}")
    r = _client.patch(f"/api/git-repo/{OWNER}/{REPO}/issues/7", json={"labels": "bug,P1"})
    assert r.status_code == 200
    last = github_calls[-1]
    assert last["method"] == "PATCH" and last["url"].endswith("/issues/7")
    assert last["body"]["labels"] == ["bug", "P1"]


def test_github_issues_update_labels_array_direct(github_calls, _client):
    _override_user(f"github:{OWNER}/{REPO}")
    r = _client.patch(f"/api/git-repo/{OWNER}/{REPO}/issues/7", json={"labels": ["bug", "P1"]})
    assert r.status_code == 200
    assert github_calls[-1]["body"]["labels"] == ["bug", "P1"]


def test_github_pulls_update_labels_string_to_array(github_calls, _client):
    """GitHub "Update a pull request" 端点不接受 labels（422）——PR 标签必须走
    PATCH /issues/{n} 且为数组；/pulls payload 不得携带 labels 字段。"""
    _override_user(f"github:{OWNER}/{REPO}")
    r = _client.patch(f"/api/git-repo/{OWNER}/{REPO}/pulls/9", json={"labels": "bug,P1"})
    assert r.status_code == 200
    assert len(github_calls) == 2
    pulls_call, issue_call = github_calls
    assert pulls_call["method"] == "PATCH" and pulls_call["url"].endswith("/pulls/9")
    assert "labels" not in pulls_call["body"]
    assert issue_call["method"] == "PATCH" and issue_call["url"].endswith("/issues/9")
    assert issue_call["body"] == {"labels": ["bug", "P1"]}


def test_github_pulls_update_title_only_no_issue_call(github_calls, _client):
    """仅改 title/body 时不得额外触发 /issues labels 请求。"""
    _override_user(f"github:{OWNER}/{REPO}")
    r = _client.patch(f"/api/git-repo/{OWNER}/{REPO}/pulls/9", json={"title": "t"})
    assert r.status_code == 200
    assert len(github_calls) == 1
    assert github_calls[0]["url"].endswith("/pulls/9")


def test_github_empty_labels_omitted_from_payload(github_calls, _client):
    _override_user(f"github:{OWNER}/{REPO}")
    r = _client.patch(f"/api/git-repo/{OWNER}/{REPO}/issues/7", json={"labels": ",,"})
    assert r.status_code == 200
    assert "labels" not in github_calls[-1]["body"]


# ---------------- API 端点测试（GitCode 通道回归） ----------------

def test_gitcode_issues_update_labels_string_passthrough(gitcode_calls, _client):
    r = _client.patch(f"/api/git-repo/{OWNER}/{REPO}/issues/7", json={"labels": "bug,P1"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "PATCH"
    assert last["body"]["labels"] == "bug,P1"


def test_gitcode_pulls_update_labels_string_passthrough(gitcode_calls, _client):
    r = _client.patch(f"/api/git-repo/{OWNER}/{REPO}/pulls/9", json={"labels": "bug,P1"})
    assert r.status_code == 200
    last = gitcode_calls[-1]
    assert last["method"] == "PATCH"
    assert last["body"]["labels"] == "bug,P1"


def test_gitcode_issues_update_labels_list_joined(gitcode_calls, _client):
    """GitCode 通道数组直传兜底：join 为逗号字符串（与字符串透传语义一致）。"""
    r = _client.patch(f"/api/git-repo/{OWNER}/{REPO}/issues/7", json={"labels": ["bug", "P1"]})
    assert r.status_code == 200
    assert gitcode_calls[-1]["body"]["labels"] == "bug,P1"


# ---------------- Pydantic 模型放宽 ----------------

def test_models_accept_str_and_list():
    assert IssueUpdateBody(labels="bug").labels == "bug"
    assert IssueUpdateBody(labels=["bug", "P1"]).labels == ["bug", "P1"]
    assert PRUpdateBody(labels="bug").labels == "bug"
    assert PRUpdateBody(labels=["bug", "P1"]).labels == ["bug", "P1"]


def test_model_rejects_other_types():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        IssueUpdateBody(labels=123)
    with pytest.raises(ValidationError):
        IssueUpdateBody(labels=["bug", 123])
    with pytest.raises(ValidationError):
        PRUpdateBody(labels=["bug", 123])
