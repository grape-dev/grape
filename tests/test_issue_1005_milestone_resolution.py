"""Issue #1005：milestone_id 取值口径（id 优先、number 兼容）与失败语义分流。

- milestones 列表/创建/更新返回补 `id`（取 url 尾部数字）。
- issues update 先解析 milestone_id（id 优先、number 兜底），再以解析出的上游 number 写入。
- 失败语义：不存在 → 400；无权限 → 403；PATCH 200 但回读不一致 → 422（不再笼统 502）。
- issues get 回显的 milestone 同样补 id，供 QA 断言。
"""
import importlib

import pytest
from fastapi import HTTPException

gra = importlib.import_module("src.api.git_repo_api")


_WEEKLY_03 = {"id": 3, "number": 850012, "title": "weekly-03"}


def _transport(monkeypatch, *, issue_get=None, milestones=None, patch_resp=None,
               milestones_error=None):
    calls = []

    def fake_req(provider, method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            if "/milestones" in url:
                if milestones_error is not None:
                    raise milestones_error
                return milestones if milestones is not None else []
            return issue_get if issue_get is not None else {}
        return patch_resp if patch_resp is not None else {"number": 7}

    monkeypatch.setattr(gra, "_vcs_req", fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tok")
    return calls


def test_milestone_id_prefers_id_over_number(monkeypatch):
    """同一数值同时命中某条的 id 与另一条的 number 时，id 优先。"""
    calls = _transport(
        monkeypatch,
        issue_get={"title": "t", "milestone": {"number": 999, "title": "by-id"}},
        milestones=[
            {"id": 5, "number": 850012, "title": "by-number"},
            {"id": 850012, "number": 999, "title": "by-id"},
        ],
    )
    gra.update_issue("o", "r", 7, gra.IssueUpdateBody(milestone_id=850012),
                     user={"auth_type": "session_token"}, p="gitcode")
    payload = next(c for c in calls if c[0] == "PATCH")[2]
    assert payload["milestone"] == 999  # id=850012 对应 number=999


def test_milestone_id_number_compat(monkeypatch):
    """传 number（历史口径）仍可用：解析后以 number 写上游。"""
    calls = _transport(
        monkeypatch,
        issue_get={"title": "t", "milestone": {"number": 850012, "title": "weekly-03"}},
        milestones=[_WEEKLY_03],
    )
    gra.update_issue("o", "r", 7, gra.IssueUpdateBody(milestone_id=850012),
                     user={"auth_type": "session_token"}, p="gitcode")
    payload = next(c for c in calls if c[0] == "PATCH")[2]
    assert payload["milestone"] == 850012


def test_milestone_permission_denied_propagates_403(monkeypatch):
    """里程碑列表 403（无权限）→ 原样 403，不落成 400/422/502。"""
    _transport(monkeypatch, milestones_error=HTTPException(
        status_code=403, detail={"code": 403, "message": "forbidden"}))
    with pytest.raises(HTTPException) as ei:
        gra.update_issue("o", "r", 7, gra.IssueUpdateBody(milestone_id=3),
                         user={"auth_type": "session_token"}, p="gitcode")
    assert ei.value.status_code == 403


def test_get_issue_echoes_milestone_with_id(monkeypatch):
    """issues get 回显 milestone，id 由 url 尾部补齐。"""
    _transport(monkeypatch, issue_get={
        "number": 7,
        "milestone": {"number": 850012, "title": "weekly-03",
                      "url": "https://api.gitcode.com/api/v5/repos/o/r/milestones/3"},
    })
    resp = gra.get_issue("o", "r", 7, user={"auth_type": "session_token"}, p="gitcode")
    assert resp["milestone"]["id"] == 3
    assert resp["milestone"]["number"] == 850012


def test_normalize_milestone_id_from_url_tail():
    assert gra._milestone_id_from_url("https://x/api/v5/repos/o/r/milestones/3") == 3
    assert gra._milestone_id_from_url(".../milestones/42?state=all") == 42
    assert gra._milestone_id_from_url(None) is None
    assert gra._milestone_id_from_url("https://x/api/v5/repos/o/r/issues/3") is None



@pytest.fixture
def api_client(monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.deps import get_current_user_or_api_key
    from src.api.main import app

    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 1, "username": "u", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "repo_key": "gitcode:o/hello"}
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "tok")
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_milestones_list_adds_id_from_url(api_client, monkeypatch):
    raw = [{"number": 850012, "title": "weekly-03", "state": "active",
            "url": "https://api.gitcode.com/api/v5/repos/o/hello/milestones/3"}]

    def _fake_req(method, url, token="", body=None):
        return raw if method == "GET" else {}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = api_client.get("/api/git-repo/o/hello/milestones?state=all")
    assert r.status_code == 200
    item = r.json()[0]
    assert item["id"] == 3
    assert item["number"] == 850012
    assert item["state"] == "open"


def test_milestones_update_adds_id_from_url(api_client, monkeypatch):
    raw = {"number": 850012, "title": "weekly-03", "state": "active",
           "url": "https://api.gitcode.com/api/v5/repos/o/hello/milestones/3"}

    def _fake_req(method, url, token="", body=None):
        return raw

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = api_client.patch("/api/git-repo/o/hello/milestones/3", json={"state": "closed"})
    assert r.status_code == 200
    assert r.json()["id"] == 3
    assert r.json()["state"] == "open"


def test_issues_update_openapi_documents_id_first(api_client):
    schema = api_client.app.openapi()
    op = schema["paths"]["/api/git-repo/{owner}/{repo}/issues/{number}"]["patch"]
    desc = op["description"]
    assert "400" in desc and "403" in desc and "422" in desc
    props = schema["components"]["schemas"]["IssueUpdateBody"]["properties"]
    assert "prefers `id`" in props["milestone_id"]["description"]