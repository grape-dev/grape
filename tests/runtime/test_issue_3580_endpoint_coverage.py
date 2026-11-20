"""Issue #3580 / PR #1868 复审修复的回归用例（第二批：单源 + 全入口入队）。

覆盖：
1. GitLab hooks 单源：mgmt 托管三件套与 GitLabProvider 同名方法发出**同一请求**；
2. gitcode / github 专用端点验签后先入队（幂等、响应保留 status="ok"）；
3. gitee/gitlab 走通用路由 = 同一 persist_webhook_event 入口（间接由 1/2 覆盖）。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.domains.mgmt.user import repo_manager as rm
from src.domains.runtime.routes import webhook as wh
from src.domains.runtime.vcs.platform_card import build_platform_card
from src.kernel import vcs_gitlab_hooks as hooks
from src.kernel.session_manager import db, db_lock
from src.kernel.token_resolver import GitLabProvider


INSTANCE_CARD = {
    "name": "GitLab (corp)", "dialect": "gitlab", "host": "gitlab.corp.example",
    "webhook_path": "/webhook/gitlab-corp", "event_header": "X-Gitlab-Event",
    "api_base": "https://gitlab.corp.example/api/v4", "api_auth": "private_token",
    "webhook_verify": "token_compare", "clone_auth": "oauth2",
    "webhook_dialect": "gitlab_v4", "token_storage_field": "", "webhook_token_field": "",
}


def test_gitlab_hooks_single_source_parity(monkeypatch):
    """mgmt 三件套与 provider 方法必须发出完全相同的 URL/body（单源）。"""
    calls = []

    def fake_request(method, url, token="", body=None, timeout=30):
        calls.append({"method": method, "url": url, "token": token, "body": body})
        return {"id": 7} if method != "GET" else []

    monkeypatch.setattr(hooks, "_request", fake_request)

    card = build_platform_card("gitlab-corp", dict(INSTANCE_CARD))
    provider = GitLabProvider(card)

    rid_a = rm.setup_gitlab_webhook("tok", "grp/sub", "proj", "https://gw/webhook/gitlab-corp",
                                    webhook_password="shared-secret",
                                    api_base=card.api_base)
    rid_b = provider.setup_webhook("tok", "grp/sub", "proj", "https://gw/webhook/gitlab-corp",
                                   "shared-secret")
    assert rid_a == rid_b == 7
    assert len(calls) == 2
    assert calls[0] == calls[1], "托管侧与 provider 侧请求不一致（实现分叉）"
    assert calls[0]["url"] == (
        "https://gitlab.corp.example/api/v4/projects/grp%2Fsub%2Fproj/hooks")
    assert calls[0]["body"]["token"] == "shared-secret"

    # remove 侧同样单源 + 404 幂等
    calls.clear()
    monkeypatch.setattr(hooks, "_request",
                        lambda *a, **k: (_ for _ in ()).throw(
                            hooks.GitLabHookError("DELETE", "u", 404, "gone")))
    # 两侧同源 → 404 都按幂等处理（返回 True），不再是"一侧 True 一侧 False"的分叉
    assert rm.remove_gitlab_webhook("tok", "g", "r", 3, api_base=card.api_base) is True
    assert provider.remove_webhook("tok", "g", "r", 3) is True


def _client(monkeypatch, provider: str):
    monkeypatch.setattr(wh, "_expected_webhook_token", lambda repo, prov: "sekret")
    app = FastAPI()
    app.include_router(wh.router)
    return TestClient(app)


def _clean_inbox(repo_prefix: str):
    with db_lock:
        db.execute("DELETE FROM grape_t_webhook_inbox WHERE repo_key LIKE ?", (repo_prefix + "%",))
        db.commit()


@pytest.mark.parametrize("provider,repo,event_header,hdr_name", [
    ("gitcode", "acme/demo", "issue_opened", "X-GitCode-Token"),
    ("github", "acme/demo", "issues", "X-GitHub-Token"),
])
def test_dedicated_endpoints_enqueue(monkeypatch, provider, repo, event_header, hdr_name):
    """gitcode/github 专用端点：验签通过后先入队，重复投递返回 duplicate。"""
    client = _client(monkeypatch, provider)
    payload = {"repository": {"full_name": repo}, "action": "opened"}
    headers = {hdr_name: "sekret", "X-GitHub-Event" if provider == "github" else "X-Gitcode-Event": event_header}
    if provider == "github":
        del headers["X-GitHub-Event"]
        headers["X-GitHub-Event"] = event_header

    _clean_inbox(f"{provider}:{repo}")
    r1 = client.post(f"/webhook/{provider}", content=json.dumps(payload).encode(), headers=headers)
    assert r1.status_code == 200
    body = r1.json()
    assert body["status"] == "ok" and body.get("inbox_id")
    assert body["duplicate"] is False

    with db_lock:
        row = db.execute("SELECT repo_key, provider, status FROM grape_t_webhook_inbox "
                         "WHERE repo_key=?", (f"{provider}:{repo}",)).fetchone()
    assert row and row[1] == provider and row[2] == "pending"

    # 同 body（无投递 ID 头 → body sha256 幂等键）重复投递
    r2 = client.post(f"/webhook/{provider}", content=json.dumps(payload).encode(), headers=headers)
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    with db_lock:
        cnt = db.execute("SELECT COUNT(*) FROM grape_t_webhook_inbox WHERE repo_key=?",
                         (f"{provider}:{repo}",)).fetchone()[0]
    assert cnt == 1
    _clean_inbox(f"{provider}:{repo}")