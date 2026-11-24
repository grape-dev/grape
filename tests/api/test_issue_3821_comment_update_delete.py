"""Issue #3821：评论删除/更新 API 闭环.

覆盖：
- issues 评论 PATCH/DELETE 路由（此前恒 405），数字 note_id 与十六进制 id 两种形态；
- pulls 评论按创建同源通道选路：合入前 /pulls 通道、合入后 #754 回退 /issues 通道；
- 非法/不可解析 id 返回可读 4xx；非 note-type 400 不错误回退；
- 契约 operationId 与 path 登记。
"""
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.domains.mgmt.user.repo_manager import UpstreamAPIError

UID = 98766
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_3821"
BASE = f"/api/git-repo/{OWNER}/{REPO}"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": f"gitcode:{OWNER}/{REPO}"}
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token", lambda *a, **k: BOT)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _mock_req(monkeypatch):
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    return recorded


def _path(rec):
    return rec["url"].split("?")[0]


def test_issues_comments_numeric_create_update_delete(monkeypatch, _client):
    """issues 评论 create→update→delete 闭环，数字 note_id 直传。"""
    recorded = _mock_req(monkeypatch)

    r = _client.post(f"{BASE}/issues/5/comments", json={"body": "first"})
    assert r.status_code == 200
    assert recorded[-1]["method"] == "POST"
    assert _path(recorded[-1]).endswith("/issues/5/comments")

    r = _client.patch(f"{BASE}/issues/5/comments/202860901", json={"body": "corrected"})
    assert r.status_code == 200
    assert recorded[-1]["method"] == "PATCH"
    assert _path(recorded[-1]).endswith("/issues/comments/202860901")
    assert recorded[-1]["body"] == {"body": "corrected"}

    r = _client.delete(f"{BASE}/issues/5/comments/202860901")
    assert r.status_code == 200
    assert recorded[-1]["method"] == "DELETE"
    assert _path(recorded[-1]).endswith("/issues/comments/202860901")


def test_issues_comments_hex_id_update_delete(monkeypatch, _client):
    """issues 评论十六进制 id：单评论查询解析为数字 note_id 后写上游。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        if method == "GET" and _path({"url": url}).endswith("/issues/comments/0df1a2"):
            return {"id": 202860902, "body": "old"}
        return {"id": 202860902, "body": (body or {}).get("body", "")}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)

    r = _client.patch(f"{BASE}/issues/5/comments/0df1a2", json={"body": "corrected"})
    assert r.status_code == 200
    assert recorded[0]["method"] == "GET"
    assert _path(recorded[0]).endswith("/issues/comments/0df1a2")
    assert recorded[-1]["method"] == "PATCH"
    assert _path(recorded[-1]).endswith("/issues/comments/202860902")

    r = _client.delete(f"{BASE}/issues/5/comments/0df1a2")
    assert r.status_code == 200
    assert recorded[-1]["method"] == "DELETE"
    assert _path(recorded[-1]).endswith("/issues/comments/202860902")


def test_issues_comments_invalid_id_readable_422(monkeypatch, _client):
    """非法 comment_id → 可读 422，且不触达上游。"""
    recorded = _mock_req(monkeypatch)
    r = _client.patch(f"{BASE}/issues/5/comments/zz-9x!", json={"body": "x"})
    assert r.status_code == 422
    assert "note_id" in r.json()["detail"]["message"]
    assert recorded == []


def test_issues_comments_unresolvable_hex_422(monkeypatch, _client):
    """十六进制 id 在 issue 通道解析不到 → 可读 422。"""
    def _fake_req(method, url, token="", body=None):
        if method == "GET" and _path({"url": url}).endswith("/issues/comments/abcdef"):
            return {"ok": True}
        if method == "GET" and _path({"url": url}).endswith("/issues/5/comments"):
            return []
        return {"ok": True}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.delete(f"{BASE}/issues/5/comments/abcdef")
    assert r.status_code == 422
    assert "note_id" in r.json()["detail"]["message"]


def test_pulls_comments_numeric_issue_note_falls_back(monkeypatch, _client):
    """合入后 #754 回退 /issues 创建的 issue note：数字 id 400 note-type → /issues 通道。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        if method == "PATCH" and _path({"url": url}).endswith("/pulls/comments/202860903"):
            raise UpstreamAPIError(method, url, 400, '{"error_message":"Note type is not correct"}')
        return {"id": 202860903, "body": (body or {}).get("body", "")}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)

    r = _client.patch(f"{BASE}/pulls/9/comments/202860903", json={"body": "corrected"})
    assert r.status_code == 200
    assert recorded[0]["method"] == "PATCH"
    assert _path(recorded[0]).endswith("/pulls/comments/202860903")
    assert recorded[-1]["method"] == "PATCH"
    assert _path(recorded[-1]).endswith("/issues/comments/202860903")
    assert recorded[-1]["body"] == {"body": "corrected"}


def test_pulls_comments_hex_issue_note_resolves_in_issue_scope(monkeypatch, _client):
    """合入后回退创建的 issue note：十六进制 id 在 issue scope 解析并按 issue 通道写入。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        p = _path({"url": url})
        if method == "GET" and p.endswith("/pulls/comments/ab12cd"):
            return {"ok": True}
        if method == "GET" and p.endswith("/pulls/9/comments"):
            return []
        if method == "GET" and p.endswith("/issues/comments/ab12cd"):
            return {"id": 202860904}
        return {"id": 202860904, "body": (body or {}).get("body", "")}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)

    r = _client.delete(f"{BASE}/pulls/9/comments/ab12cd")
    assert r.status_code == 200
    last = recorded[-1]
    assert last["method"] == "DELETE"
    assert _path(last).endswith("/issues/comments/202860904")
    assert any(c["method"] == "GET" and _path(c).endswith("/issues/comments/ab12cd")
               for c in recorded)


def test_merged_pull_comment_create_update_delete_via_issue_channel(monkeypatch, _client):
    """已合入 PR：#754 回退 /issues 创建 → update/delete 走 issue note 通道闭环。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        p = _path({"url": url})
        if method == "POST" and p.endswith("/pulls/9/comments"):
            raise UpstreamAPIError(method, url, 405,
                                   "not allowed to be reviewed after being merged")
        if method == "POST" and p.endswith("/issues/9/comments"):
            return {"id": 202860905}
        if p.endswith("/pulls/comments/202860905"):
            raise UpstreamAPIError(method, url, 400,
                                   '{"error_message":"Note type is not correct"}')
        return {"id": 202860905, "body": (body or {}).get("body", "")}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)

    r = _client.post(f"{BASE}/pulls/9/comments", json={"body": "post-merge"})
    assert r.status_code == 200
    assert r.json()["id"] == 202860905

    r = _client.patch(f"{BASE}/pulls/9/comments/202860905", json={"body": "corrected"})
    assert r.status_code == 200
    assert recorded[-1]["method"] == "PATCH"
    assert _path(recorded[-1]).endswith("/issues/comments/202860905")

    r = _client.delete(f"{BASE}/pulls/9/comments/202860905")
    assert r.status_code == 200
    assert recorded[-1]["method"] == "DELETE"
    assert _path(recorded[-1]).endswith("/issues/comments/202860905")


def test_pulls_comments_other_400_not_fallback(monkeypatch, _client):
    """非 note-type 的 400 原样透传，不错误回退掩盖真实错误。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        raise UpstreamAPIError(method, url, 400, '{"error_message":"body cannot be empty"}')

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)

    r = _client.patch(f"{BASE}/pulls/9/comments/202860906", json={"body": "x"})
    assert r.status_code == 400
    assert len(recorded) == 1
    assert _path(recorded[0]).endswith("/pulls/comments/202860906")


def test_issue_comments_update_delete_contract_operations():
    """新 issues 评论 PATCH/DELETE 进契约，CLI 可据此生成 update/delete 子命令。"""
    from src.api.openapi import custom_openapi
    schema = custom_openapi(app)
    cp = "/api/git-repo/{owner}/{repo}/issues/{number}/comments/{comment_id}"
    assert cp in schema["paths"]
    assert schema["paths"][cp]["patch"]["operationId"] == "gitrepo_issues_comments_update"
    assert schema["paths"][cp]["delete"]["operationId"] == "gitrepo_issues_comments_delete"
    assert schema["paths"][cp]["patch"].get("x-token-scope") == "session_only"
    assert schema["paths"][cp]["delete"].get("x-token-scope") == "session_only"