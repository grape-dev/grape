"""B3②：代理 PATCH PR 关闭（state 透传）隔离测试。

背景：PRUpdateBody 原先未声明 state 字段——pydantic 静默丢弃后 payload 为空：
- github 上游 400 'Body should be a JSON object'（PATCH 空 body）
- gitee/gitcode v5 200 但 PR state 仍 open（无字段可更新，纯 no-op）

修复：PRUpdateBody 增加 state（open/closed），透传进上游 PATCH /pulls body。
本测试 monkeypatch _vcs_req 捕获真实发出的 payload（不触网）。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_b3_pr_state.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi.testclient import TestClient

import src.api.git_repo_api as gra
from src.api.main import app
from src.api.db_setup import init_database

init_database()

_client = TestClient(app)

_calls = []


def _fake_vcs_req(provider, method, url, token, body=None):
    _calls.append({"provider": provider, "method": method, "url": url, "body": body})
    return {"number": 1, "state": (body or {}).get("state", "open"), "title": "t"}


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    _calls.clear()
    monkeypatch.setattr(gra, "_vcs_req", _fake_vcs_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "fake-token")
    from src.api.deps import get_current_user_or_api_key
    app.dependency_overrides[gra.get_current_user_or_api_key] = lambda: {"id": 1, "auth_type": "jwt"}
    app.dependency_overrides[gra._provider] = lambda: "gitcode"
    yield
    app.dependency_overrides.clear()


def _patch(payload):
    return _client.patch("/api/git-repo/example-user/demo/pulls/1", json=payload)


def test_state_closed_forwarded():
    r = _patch({"state": "closed"})
    assert r.status_code == 200, r.text
    pull_bodies = [c["body"] for c in _calls if c["url"].endswith("/pulls/1")]
    assert pull_bodies and pull_bodies[0] == {"state": "closed"}, _calls
    assert r.json()["state"] == "closed"


def test_state_only_not_empty_body():
    """原先空 payload → github 400；现仅 state 也产生非空 body。"""
    _patch({"state": "closed"})
    assert _calls[0]["body"], "PATCH body 不能为空（github 400 根因）"


def test_state_open_forwarded():
    _patch({"state": "open", "title": "T"})
    assert _calls[0]["body"] == {"title": "T", "state": "open"}


def test_invalid_state_400():
    r = _patch({"state": "merged"})
    assert r.status_code == 400
    assert "invalid state" in r.text


def test_no_state_omitted():
    _patch({"title": "T"})
    assert _calls[0]["body"] == {"title": "T"}


@pytest.fixture(autouse=True, scope="module")
def _restore_shared_db_env():
    yield
    os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")
