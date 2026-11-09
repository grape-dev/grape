"""Issue #754：已 merged PR 评论 405 → 等价留痕通道（/issues/{n}/comments）兜底.

场景覆盖（验收③④）：
- GitCode 网关端点：POST /pulls/{n}/comments 405（merged PR 不可 review）→ 自动降级
  POST /issues/{n}/comments，响应回传评论对象，台账照记（双向可追溯）。
- 内部 QA 回帖路径 `_post_result_to_pr`：urllib 405 → 同源降级。
"""
import io
import json as _json
import urllib.error
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.user.repo_manager import UpstreamAPIError

UID = 98755
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_754"

base = f"/api/git-repo/{OWNER}/{REPO}"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": f"gitcode:{OWNER}/{REPO}"}
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_pull_comment_405_falls_back_to_issue_channel(monkeypatch, _client):
    """GitCode PR 评论 405（已 merged）→ 改走 /issues/{n}/comments 等价通道。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        if method == "POST" and url.endswith("/pulls/9/comments"):
            raise UpstreamAPIError(method, url, 405,
                                   '{"message":"not allowed to be reviewed after being merged"}')
        if method == "POST" and url.endswith("/issues/9/comments"):
            return {"id": 424242, "body": body["body"]}
        return {"ok": True}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    ledger = []
    monkeypatch.setattr("src.outbound_ledger.record_outbound",
                        lambda *a, **k: ledger.append((a, k)) or True)

    r = _client.post(f"{base}/pulls/9/comments", json={"body": "post-merge trace"})
    assert r.status_code == 200
    assert r.json()["id"] == 424242

    posts = [c for c in recorded if c["method"] == "POST"]
    assert any(c["url"].endswith("/pulls/9/comments") for c in posts)
    fallback = [c for c in posts if c["url"].endswith("/issues/9/comments")]
    assert fallback and fallback[0]["body"]["body"].endswith("\n<!-- grape-auto-comment -->")
    # 留痕台账（issue_comment + content_hash，双向可追溯）
    assert ledger and ledger[0][1].get("content_hash")


def test_pull_comment_other_errors_not_fallback(monkeypatch, _client):
    """非 405（如 403）不触发降级，保持上游透传语义。"""
    def _fake_req(method, url, token="", body=None):
        if method == "POST":
            raise UpstreamAPIError(method, url, 403, '{"message":"Forbidden"}')
        return {"ok": True}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = _client.post(f"{base}/pulls/9/comments", json={"body": "x"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == 403


def _http_error_405(url):
    return urllib.error.HTTPError(url, 405, "not allowed to be reviewed after being merged",
                                  {}, io.BytesIO(b"{}"))


def test_post_result_to_pr_405_fallback(monkeypatch):
    """内部 QA 回帖路径：GitCode pulls 评论 405 → issues 评论通道兜底。"""
    from src.api.event_pipeline import _post_result_to_pr

    calls = []

    def _fake_urlopen(req, timeout=15):
        calls.append(req.full_url)
        if req.full_url.endswith("/pulls/33/comments"):
            raise _http_error_405(req.full_url)
        return io.BytesIO(_json.dumps({"id": 777}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("src.api.event_pipeline._ledger_record_comment",
                        lambda *a, **k: True)

    _post_result_to_pr("o/hello", "33", '{"conclusion": "trace after merge"}',
                       BOT, "qa", repo_key="gitcode:o/hello", agent_result=False)

    assert calls[0].endswith("/pulls/33/comments")
    assert calls[1].endswith("/issues/33/comments")
