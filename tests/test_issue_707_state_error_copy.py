"""Issue #707：issues update `state` 非法值报错文案与契约字段对齐（state vs state_event）。

- 非法 state → 本地 400，文案字段名为 `state`、列出合法取值，无 `state_event` 字样。
- 合法中性值（open/opened/reopen/closed/close）→ 翻译为上游合法事件值（gitcode: reopen/close）。
- update_issue 对 state 统一 strip().lower() 归一（大小写旁路不再透传上游）。
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.vcs.providers import gitcode, github

UID = 70701
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_707"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": f"gitcode:{OWNER}/{REPO}"}
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)
    monkeypatch.setattr("src.user.db.get_github_token", lambda *a, **k: BOT)


@pytest.fixture
def gitcode_calls(monkeypatch):
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        # title 回取（#640 PATCH 必填 title 自动回填）
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}, "title": "t"}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    return recorded


# ---------- 翻译层映射（provider 值映射收敛） ----------

@pytest.mark.parametrize("neutral,expected", [
    ("open", "reopen"), ("opened", "reopen"), ("reopen", "reopen"),
    ("closed", "close"), ("close", "close"),
])
def test_gitcode_update_state_translation(neutral, expected):
    payload = gitcode.translate("issues", "update", {"state": neutral}, {})
    assert payload["state"] == expected


@pytest.mark.parametrize("neutral,expected", [
    ("open", "open"), ("opened", "open"), ("reopen", "open"),
    ("closed", "closed"), ("close", "closed"),
])
def test_github_update_state_translation(neutral, expected):
    payload = github.translate("issues", "update", {"state": neutral}, {})
    assert payload["state"] == expected


# ---------- API 级：非法 state 本地 400（无 state_event 字样） ----------

@pytest.mark.parametrize("bad", ["openn", "invalidvalue", "state", "ALL"])
def test_update_issue_invalid_state_local_400(gitcode_calls, _client, bad):
    resp = _client.patch(
        f"/api/git-repo/{OWNER}/{REPO}/issues/1", json={"state": bad})
    assert resp.status_code == 400
    msg = str(resp.json())
    assert "'state'" in msg
    assert "reopen" in msg and "close" in msg
    assert "state_event" not in msg
    # 本地拒绝：不应有任何上游调用（含 title 回取）
    assert gitcode_calls == []


# ---------- API 级：合法中性值翻译后发上游 ----------

@pytest.mark.parametrize("state,upstream", [
    ("open", "reopen"), ("opened", "reopen"), ("closed", "close"),
])
def test_update_issue_state_alias_translated(gitcode_calls, _client, state, upstream):
    resp = _client.patch(
        f"/api/git-repo/{OWNER}/{REPO}/issues/1", json={"state": state})
    assert resp.status_code == 200
    patch_calls = [c for c in gitcode_calls if c["method"] == "PATCH"]
    assert patch_calls, "PATCH should reach upstream"
    assert patch_calls[-1]["body"]["state"] == upstream


def test_update_issue_state_case_normalized(gitcode_calls, _client):
    resp = _client.patch(
        f"/api/git-repo/{OWNER}/{REPO}/issues/1", json={"state": "Reopen"})
    assert resp.status_code == 200
    patch_calls = [c for c in gitcode_calls if c["method"] == "PATCH"]
    assert patch_calls[-1]["body"]["state"] == "reopen"
