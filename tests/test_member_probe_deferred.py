"""/repos/member 探活去请求化（member-repo-probe-latency-fix）TDD 测试。

🔴 红阶段：以下测试在修复前 FAIL——
- 缓存命中仍会启动 full probe（_probe_stale_repos 被调）
- 缓存命中路径会调 with_oauth_token 取探活 token
- 无节流，重复调用都探活
🟢 绿阶段：修复后全部 PASS。

敏感信息：无真实仓库名/IP/凭据（使用测试数据与 mock）。
"""
import os
import sys

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.main import app
from src.session_manager import db, db_lock

client = TestClient(app)
_headers = {}


def _seed_user_and_cache():
    """预置一个测试用户 + user_repos 缓存（未过期），使 /member 走缓存命中路径。"""
    global _headers
    from src.auth.grape_account_db import create_grape_account, get_token_version
    from src.api.jwt_utils import create_access_token
    from src.user.db import save_user_repos

    uname = "probe_ttl_user"
    from src.auth.grape_account_db import get_grape_account_by_username
    acc = get_grape_account_by_username(uname)
    if not acc:
        acc = create_grape_account(username=uname, email="", password="pw")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM grape_accounts WHERE username=?", (uname,))
        row = c.fetchone()
        uid = row[0]
        c.execute("DELETE FROM external_identities WHERE grape_user_id=? AND provider='gitcode'", (uid,))
        c.execute(
            "INSERT OR REPLACE INTO external_identities "
            "(grape_user_id, provider, external_id, external_login, external_name, access_token, refresh_token, status) "
            "VALUES (?, 'gitcode', ?, ?, ?, ?, ?, 1)",
            (uid, "probe-external-id", uname, uname, "test-token", "test-refresh"))
        db.commit()
    tv = get_token_version(uid)
    token = create_access_token(uid, uname, tv)
    _headers = {"Authorization": f"Bearer {token}"}
    save_user_repos(uid, [
        {"repo_key": "gitcode:ownorg/probe-a", "full_name": "ownorg/probe-a",
         "owner": "ownorg", "name": "probe-a", "is_admin": True, "role": "Admin"},
        {"repo_key": "gitcode:ownorg/probe-b", "full_name": "ownorg/probe-b",
         "owner": "ownorg", "name": "probe-b", "is_admin": False, "role": "Member"},
    ])
    # 强制 synced_at 为「本地时间现在」，确保缓存被判定为未过期
    # （_is_expired 用 datetime.now() 本地时间与 synced_at 字符串（视为本地）比较；
    #    SQLite datetime('now') 是 UTC，会误判过期，故用 Python 本地时间写入）
    from datetime import datetime as _dt
    with db_lock:
        db.execute("UPDATE user_repos SET synced_at=? WHERE user_id=?", (_dt.now().strftime("%Y-%m-%d %H:%M:%S"), uid))
        db.commit()
    return uid


def _clear_member_cache():
    from src.user.db import clear_user_repos
    from src.auth.grape_account_db import get_grape_account_by_username
    acc = get_grape_account_by_username("probe_ttl_user")
    if acc:
        clear_user_repos(acc["id"])


class TestMemberProbeDeferred:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        uid = _seed_user_and_cache()
        self.uid = uid
        # 记录 _probe_stale_repos 是否被触发
        self.probe_called = []
        monkeypatch.setattr("src.api.routes.repos._probe_stale_repos",
                            lambda *a, **k: self.probe_called.append((a, k)))
        # 记录 _fetch_and_cache（VCS 全量拉取）是否被调用——缓存命中不应调用
        self.fetch_calls = []

        def _fake_fetch(uid, tok, p):
            self.fetch_calls.append(p)
            return [
                {"repo_key": "gitcode:ownorg/probe-a", "full_name": "ownorg/probe-a",
                 "owner": "ownorg", "name": "probe-a", "is_admin": True, "role": "Admin"},
                {"repo_key": "gitcode:ownorg/probe-b", "full_name": "ownorg/probe-b",
                 "owner": "ownorg", "name": "probe-b", "is_admin": False, "role": "Member"},
            ]

        monkeypatch.setattr("src.api.routes.repos._fetch_and_cache", _fake_fetch)
        yield
        _clear_member_cache()

    def test_ut01_cache_hit_does_not_trigger_probe(self):
        """缓存命中时不应启动 full probe（探活去请求化）。"""
        r = client.get("/api/repos/member", headers=_headers)
        assert r.status_code == 200
        data = r.json()
        assert data.get("_cached") is True
        # 修复前：缓存命中仍触发 probe → probe_called 非空；修复后应为空
        assert self.probe_called == [], "cache-hit should NOT start probe"

    def test_ut02_cache_hit_no_vcs_fetch(self):
        """缓存命中路径不应调用 _fetch_and_cache（VCS 全量拉取）。"""
        r = client.get("/api/repos/member", headers=_headers)
        assert r.status_code == 200
        assert r.json().get("_cached") is True
        assert self.fetch_calls == [], "cache-hit should not fetch from VCS"

    def test_ut03_cache_expired_triggers_probe(self):
        """缓存过期/缺失时才触发探活（数据刷新后清理失效托管）。"""
        from src.auth.grape_account_db import get_grape_account_by_username
        from src.user.db import clear_user_repos
        acc = get_grape_account_by_username("probe_ttl_user")
        if acc:
            clear_user_repos(acc["id"])
        # fixture 的 _fake_fetch 返回两条 gitcode 仓库 → 缓存过期路径成功，"探活"被触发
        r = client.get("/api/repos/member", headers=_headers)
        assert r.status_code == 200
        assert self.fetch_calls, "cache-miss path should fetch from VCS"
        # 探活是后台线程异步启动，轮询等待其到达 _probe_stale_repos
        import time as _time
        deadline = _time.time() + 2
        while not self.probe_called and _time.time() < deadline:
            _time.sleep(0.05)
        assert self.probe_called, "cache-miss path should start probe"


class TestProbeThrottle:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        uid = _seed_user_and_cache()
        self.uid = uid
        _clear_member_cache()  # 强制缓存过期，走探活触发分支
        self.calls = []
        monkeypatch.setattr("src.api.routes.repos._probe_stale_repos",
                            lambda *a, **k: self.calls.append(1))
        monkeypatch.setattr("src.api.routes.repos._fetch_and_cache",
                            lambda uid, tok, p: [
                                {"repo_key": "gitcode:ownorg/probe-a", "full_name": "ownorg/probe-a",
                                 "owner": "ownorg", "name": "probe-a", "is_admin": True, "role": "Admin"},
                                {"repo_key": "gitcode:ownorg/probe-b", "full_name": "ownorg/probe-b",
                                 "owner": "ownorg", "name": "probe-b", "is_admin": False, "role": "Member"}])
        yield
        _clear_member_cache()

    def test_ut05_throttle_within_interval(self):
        """节流：短时间重复请求只触发一次探活。"""
        with patch("src.user.repo_manager._req", return_value={}):
            client.get("/api/repos/member", headers=_headers)
            client.get("/api/repos/member", headers=_headers)
        # 修复前每次 cache-miss 都探活 → >=2；修复后节流应只有 1 次
        assert len(self.calls) <= 1, f"probe should be throttled, got {len(self.calls)}"