"""托管仓库平台路由修复回归测试（docs/weekly-08/host-repo-provider-routing-fix-design.md）。

背景：host_repo 的 provider 判定原按「登录平台 login_provider」短路，导致 GitHub 仓库被当 GitCode 托管
（gitcode.com/.../hooks 401 或 404）。本文件覆盖：显式 provider、仓库来源提示(前缀/managed)、
github 无 token 直接 400、404/401 文案区分、login_provider 不再短路。
"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.deps import require_no_session_token
from src.session_manager import db, db_lock

client = TestClient(app)

_HEADERS_GH = {}
_HEADERS_GC = {}


def _cleanup():
    with db_lock:
        c = db.cursor()
        for t in ("managed_repos",):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE '%testhost%' OR full_name LIKE '%testhost%'")
            except Exception:
                pass
        db.commit()


def setup_module():
    global _HEADERS_GH, _HEADERS_GC
    try:
        from src.api import db_setup
        db_setup.init_database()
    except Exception:
        pass
    _cleanup()
    app.dependency_overrides[require_no_session_token] = lambda: {
        "id": 2, "username": "Jason7602", "login_provider": "gitcode"}
    _HEADERS_GH = {"Authorization": "Bearer fake-jwt"}


def teardown_module():
    app.dependency_overrides.clear()
    _cleanup()


def _records():
    return {"gh": 0, "gc": 0}


def _patch_base(mp, rec):
    mp.setattr("src.user.db.get_webhook_url",
               lambda: "https://grape.topxtopx.com/webhook/gitcode")
    mp.setattr("src.api.session_store.ensure_default_agent_sessions", lambda *a, **k: None)
    mp.setattr("src.api.routes.repos.with_oauth_token", lambda *a, **k: None)
    mp.setattr("src.user.db.get_global_github_bot_token", lambda: "")
    mp.setattr("src.user.db.get_global_github_bot_username", lambda: "")
    def f_gh(*a, **k):
        rec["gh"] += 1
        return 101
    def f_gc(*a, **k):
        rec["gc"] += 1
        return 202
    mp.setattr("src.user.repo_manager.setup_github_webhook", f_gh)
    mp.setattr("src.api.routes.repos.setup_webhook", f_gc)
    mp.setattr("src.api.routes.repos.find_existing_webhook", lambda *a, **k: None)


def test_HT01_explicit_provider_github_only_hits_github(monkeypatch):
    rec = _records()
    _patch_base(monkeypatch, rec)
    monkeypatch.setattr("src.api.routes.repos.get_user_github_token_with_refresh", lambda u, user_id: "gh_tok")
    r = client.post("/api/repos/host?provider=github",
                    json={"full_name": "Jason7602/testhost"}, headers=_HEADERS_GH)
    assert r.status_code == 200, r.text
    assert rec["gh"] == 1 and rec["gc"] == 0


def test_HT02_full_name_github_prefix_routes_github_not_gitcode(monkeypatch):
    rec = _records()
    _patch_base(monkeypatch, rec)
    monkeypatch.setattr("src.api.routes.repos.get_user_github_token_with_refresh", lambda u, user_id: "gh_tok")
    monkeypatch.setattr("src.api.routes.repos.get_user_token_with_refresh", lambda u, i: "gc_tok")
    r = client.post("/api/repos/host",
                    json={"full_name": "github:Jason7602/testhost"}, headers=_HEADERS_GH)
    assert r.status_code == 200, r.text
    assert rec["gh"] == 1 and rec["gc"] == 0, "github: 前缀必须定向 GitHub，不得打 GitCode"


def test_HT03_managed_github_prefix_routes_github(monkeypatch):
    rec = _records()
    _patch_base(monkeypatch, rec)
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key='github:Jason7602/testhost'")
        db.cursor().execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
            "VALUES ('github:Jason7602/testhost','Jason7602','testhost','Jason7602/testhost')")
        db.commit()
    monkeypatch.setattr("src.api.routes.repos.get_user_github_token_with_refresh", lambda u, user_id: "gh_tok")
    monkeypatch.setattr("src.api.routes.repos.get_user_token_with_refresh", lambda u, i: "gc_tok")
    try:
        r = client.post("/api/repos/host",
                        json={"full_name": "Jason7602/testhost"}, headers=_HEADERS_GH)
        assert r.status_code == 200, r.text
        assert rec["gh"] == 1 and rec["gc"] == 0
    finally:
        _cleanup()


def test_HT04_github_no_token_400_not_gitcode(monkeypatch):
    rec = _records()
    _patch_base(monkeypatch, rec)
    monkeypatch.setattr("src.api.routes.repos.get_user_github_token_with_refresh", lambda u, user_id: "")
    r = client.post("/api/repos/host?provider=github",
                    json={"full_name": "Jason7602/testhost"}, headers=_HEADERS_GH)
    assert r.status_code == 400
    assert r.json()["detail"].get("error_code") == "TOKEN_NOT_BOUND"
    assert "绑定 GitHub" in r.json()["detail"]["message"]
    assert rec["gh"] == 0 and rec["gc"] == 0, "github 无 token 不得回退 gitcode"


def test_HT05_gitcode_404_message(monkeypatch):
    rec = _records()
    _patch_base(monkeypatch, rec)
    monkeypatch.setattr("src.api.routes.repos.with_oauth_token", lambda *a, **k: {"login": "Jason7602"})
    def f_gc(*a, **k):
        rec["gc"] += 1
        raise RuntimeError("GitCode API POST .../hooks failed (404): Project not found:Jason7602/testhost")
    monkeypatch.setattr("src.api.routes.repos.setup_webhook", f_gc)
    monkeypatch.setattr("src.api.routes.repos.get_user_token_with_refresh", lambda u, i: "gc_tok")
    r = client.post("/api/repos/host?provider=gitcode",
                    json={"full_name": "Jason7602/testhost"}, headers=_HEADERS_GH)
    assert r.status_code == 502
    assert "不存在于 GitCode" in r.json()["detail"]["message"] or "Project not found" in r.json()["detail"]["message"]


def test_HT06_login_provider_gitcode_but_github_repo_goes_github(monkeypatch):
    """用户 GitCode 登录 + github: 仓库 → 不得再被 login_provider 短路到 gitcode。"""
    rec = _records()
    _patch_base(monkeypatch, rec)
    monkeypatch.setattr("src.api.routes.repos.get_user_github_token_with_refresh", lambda u, user_id: "gh_tok")
    monkeypatch.setattr("src.api.routes.repos.get_user_token_with_refresh", lambda u, i: "gc_tok")
    r = client.post("/api/repos/host",
                    json={"full_name": "github:Jason7602/testhost"}, headers=_HEADERS_GH)
    assert r.status_code == 200, r.text
    assert rec["gh"] == 1 and rec["gc"] == 0


def test_HT07_gitcode_token_401_message(monkeypatch):
    rec = _records()
    _patch_base(monkeypatch, rec)
    monkeypatch.setattr("src.api.routes.repos.with_oauth_token", lambda *a, **k: {"login": "Jason7602"})
    def f_gc(*a, **k):
        raise RuntimeError("GitCode API POST .../hooks failed (401): Bad credentials")
    monkeypatch.setattr("src.api.routes.repos.setup_webhook", f_gc)
    monkeypatch.setattr("src.api.routes.repos.get_user_token_with_refresh", lambda u, i: "gc_tok")
    r = client.post("/api/repos/host?provider=gitcode",
                    json={"full_name": "Jason7602/testhost"}, headers=_HEADERS_GH)
    assert r.status_code == 502
    assert "登录已失效" in r.json()["detail"]["message"] or "GitCode" in r.json()["detail"]["message"]