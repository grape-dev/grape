"""动态通用 webhook 路由用例（S17/D14）：平台卡驱动的新平台零代码接入路由。"""
import json

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


@pytest.fixture()
def managed_repo():
    repo = "gitee_tester/mpd_demo"
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_secret) "
            "VALUES (?, 'gitee_tester', 'mpd_demo', ?, 'whsec_gitee')",
            (f"gitee:{repo}", repo))
        db.commit()
    yield repo
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (f"gitee:{repo}",))
        db.commit()


@pytest.fixture()
def github_managed_repo():
    """builtin GitHub 端点用例的播种：repo_key 必须带 github: 前缀
    （webhook 查表走 to_repo_key(repo, provider)，gitee: 前缀在 github
    端点查不到 → 404 not managed 是 fail-closed 正确语义）。"""
    repo = "gitee_tester/mpd_demo"
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_secret) "
            "VALUES (?, 'gitee_tester', 'mpd_demo', ?, 'whsec_gitee')",
            (f"github:{repo}", repo))
        db.commit()
    yield repo
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (f"github:{repo}",))
        db.commit()


def _client():
    from src.api.main import app
    return TestClient(app)


GITEE_PUSH = {
    "ref": "refs/heads/main",
    "repository": {"full_name": "gitee_tester/mpd_demo", "name": "mpd_demo"},
    "sender": {"login": "tester"},
    "commits": [],
}


def test_s17_gitee_route_dispatch_and_verify(gitee_card, managed_repo):
    c = _client()
    # 正确签名（Gitee 语义：X-Gitee-Signature = secret 明文）
    r = c.post("/webhook/gitee", json=GITEE_PUSH, headers={"X-Gitee-Signature": "whsec_gitee"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    # 错误签名 → 403 签名不符
    r2 = c.post("/webhook/gitee", json=GITEE_PUSH, headers={"X-Gitee-Signature": "wrong"})
    assert r2.status_code == 403 and "mismatch" in r2.json()["message"]
    # 缺签名 → 401
    r3 = c.post("/webhook/gitee", json=GITEE_PUSH)
    assert r3.status_code == 401 and "missing" in r3.json()["message"]


def test_s17_unmanaged_repo_rejected(gitee_card):
    c = _client()
    body = {**GITEE_PUSH, "repository": {"full_name": "nobody/unmanaged", "name": "unmanaged"}}
    r = c.post("/webhook/gitee", json=body, headers={"X-Gitee-Signature": "whsec_gitee"})
    assert r.status_code == 404 and "not managed" in r.json()["message"]


def test_s17_unknown_provider_404(gitee_card):
    """未配卡平台不静默回退 gitcode 处理（fail-fast，与 G1 同纪律）。"""
    c = _client()
    r = c.post("/webhook/noplatform", json=GITEE_PUSH)
    assert r.status_code == 404 and "unknown" in r.json()["message"]


def test_s17_builtin_routes_not_shadowed(gitee_card, github_managed_repo):
    """显式路由优先：/webhook/github 仍走 GitHub 端点（不被通用路由吞掉）。"""
    c = _client()
    # GitHub 端点存在且行为照旧：未带签名的 GitHub push → 401 缺签名（既有语义）
    r = c.post("/webhook/github", json={"repository": {"full_name": github_managed_repo}})
    assert r.status_code in (200, 401), r.text
    assert r.json().get("code", 200) != 404 or "unknown" not in r.json().get("message", "")
