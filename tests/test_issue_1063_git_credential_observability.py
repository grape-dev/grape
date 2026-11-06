"""Issue #1063：git 凭据注入接口失败语义分级与结构化日志测试。

覆盖：
- bot token 读取异常 / user token 刷新异常 / 两者均空 三个独立 stage；
- 502 响应含 code/error_code/stage/retryable/retry_after_s，且不含 token 明文；
- auth_type != session_token 保持 403；
- 未预期异常 500 带 request_id，日志含 stage 与异常类名；
- github provider 同构分支。
"""
import logging

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app

UID = 98701
OWNER = "o"
REPO = "hello"
BOT_TOKEN = "bot_tok_SECRET_123"
USER_TOKEN = "usr_tok_SECRET_456"


def _set_user(agent_type="vod", auth_type="session_token", repo_key=None):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID,
        "username": "git_tester",
        "auth_type": auth_type,
        "agent_type": agent_type,
        "user_id": 0 if agent_type != "chat" else UID,
        "session_key": "s1",
        "repo_key": repo_key or f"gitcode:{OWNER}/{REPO}",
    }


@pytest.fixture
def client():
    _set_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _detail(resp):
    return resp.json()["detail"]


def _event_records(caplog, event):
    return [r for r in caplog.records if getattr(r, "event", "") == event]


def _log_dump(caplog):
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        for value in vars(record).values():
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 三分支独立 stage
# ---------------------------------------------------------------------------

def test_bot_lookup_exception_stage(client, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _set_user(agent_type="vod")

    def _raise(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("src.user.db.get_gitcode_token", _raise)
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh", lambda *a, **k: "")

    r = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
    assert r.status_code == 502
    d = _detail(r)
    assert d["code"] == 502
    assert d["error_code"] == "GIT_CREDENTIAL_UNAVAILABLE"
    assert d["stage"] == "bot_token_lookup"
    assert d["retryable"] is True
    assert d["retry_after_s"] > 0
    assert d["provider"] == "gitcode"

    records = _event_records(caplog, "git_credential_failed")
    assert len(records) == 1
    record = records[0]
    assert record.stage == "bot_token_lookup"
    assert record.error_code == "GIT_CREDENTIAL_UNAVAILABLE"
    assert record.provider == "gitcode"
    assert record.repo_key == f"gitcode:{OWNER}/{REPO}"
    assert record.error_class == "RuntimeError"
    assert record.auth_type == "session_token"
    assert record.agent_type == "vod"
    assert record.duration_ms >= 0


def test_user_refresh_exception_stage(client, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _set_user(agent_type="chat")

    def _raise(*a, **k):
        raise RuntimeError("oauth upstream 502")

    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "")
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh", _raise)

    r = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
    assert r.status_code == 502
    d = _detail(r)
    assert d["stage"] == "user_token_refresh"
    assert d["retryable"] is True
    assert d["retry_after_s"] == 15

    records = _event_records(caplog, "git_credential_failed")
    assert len(records) == 1
    assert records[0].stage == "user_token_refresh"
    assert records[0].error_class == "RuntimeError"


def test_both_empty_stage(client, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _set_user(agent_type="vod")
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "")
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh", lambda *a, **k: "")

    r = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
    assert r.status_code == 502
    d = _detail(r)
    assert d["stage"] == "both_unavailable"
    assert d["error_code"] == "GIT_CREDENTIAL_UNAVAILABLE"

    records = _event_records(caplog, "git_credential_failed")
    assert len(records) == 1
    assert records[0].stage == "both_unavailable"
    assert records[0].error_class == ""


# ---------------------------------------------------------------------------
# 失败响应不泄漏凭据
# ---------------------------------------------------------------------------

def test_failure_response_does_not_leak_token(client, monkeypatch):
    _set_user(agent_type="vod")
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "")
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh", lambda *a, **k: "")

    r = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
    body = r.text
    assert BOT_TOKEN not in body
    assert USER_TOKEN not in body
    assert "password" not in body
    # 异常摘要也不回显
    assert "database is locked" not in body


def test_bot_exception_message_not_in_response(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _set_user(agent_type="vod")

    def _raise(*a, **k):
        raise RuntimeError("db error token=" + BOT_TOKEN)

    monkeypatch.setattr("src.user.db.get_gitcode_token", _raise)
    monkeypatch.setattr("src.api.deps.get_user_token_with_refresh", lambda *a, **k: "")

    r = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
    assert r.status_code == 502
    # 响应与日志两条链路都不得泄漏异常原文中的 token
    assert BOT_TOKEN not in r.text
    assert BOT_TOKEN not in _log_dump(caplog)
    assert "db error token=" not in _log_dump(caplog)


# ---------------------------------------------------------------------------
# 403 语义不变
# ---------------------------------------------------------------------------

def test_non_session_token_still_403(client):
    _set_user(auth_type="api_key")
    r = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
    assert r.status_code == 403
    d = _detail(r)
    assert d["code"] == 403
    assert "会话级临时 token" in d["message"]


# ---------------------------------------------------------------------------
# 未预期异常：500 + request_id + 阶段日志
# ---------------------------------------------------------------------------

def test_unexpected_exception_500_with_request_id(caplog):
    caplog.set_level(logging.WARNING)
    _set_user(agent_type="vod")
    try:
        import src.api.routes.repos as repos_mod

        def _boom(_owner):
            raise RuntimeError("unexpected strip failure")

        orig = repos_mod._strip_provider_prefix
        repos_mod._strip_provider_prefix = _boom
        try:
            err_client = TestClient(app, raise_server_exceptions=False)
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT_TOKEN)
                r = err_client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
        finally:
            repos_mod._strip_provider_prefix = orig
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 500
    body = r.json()
    assert body["code"] == 500
    assert body["request_id"] and body["request_id"] != "-"

    records = _event_records(caplog, "git_credential_failed")
    assert len(records) == 1
    assert records[0].stage == "credential_resolve"
    assert records[0].error_class == "RuntimeError"


# ---------------------------------------------------------------------------
# github provider 同构
# ---------------------------------------------------------------------------

def test_github_bot_lookup_exception_stage(client, monkeypatch):
    _set_user(agent_type="vod", repo_key=f"github:{OWNER}/{REPO}")

    def _raise(*a, **k):
        raise RuntimeError("github db error")

    monkeypatch.setattr("src.user.db.get_github_token", _raise)
    monkeypatch.setattr("src.api.deps.get_user_github_token_with_refresh", lambda *a, **k: "")

    r = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
    assert r.status_code == 502
    d = _detail(r)
    assert d["provider"] == "github"
    assert d["stage"] == "bot_token_lookup"
    assert d["error_code"] == "GIT_CREDENTIAL_UNAVAILABLE"
    assert d["retryable"] is True


def test_success_path_still_returns_credential(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _set_user(agent_type="vod")
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT_TOKEN)

    r = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential")
    assert r.status_code == 200
    assert r.json()["password"] == BOT_TOKEN
    records = _event_records(caplog, "git_credential_ok")
    assert len(records) == 1
    assert records[0].provider == "gitcode"
    assert records[0].repo_key == f"gitcode:{OWNER}/{REPO}"
    assert records[0].duration_ms >= 0
    # 成功日志也不得出现 token 明文
    assert BOT_TOKEN not in _log_dump(caplog)