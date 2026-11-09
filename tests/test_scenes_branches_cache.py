"""Issue #542 回归测试：/api/scenes/branches TTL 缓存。

覆盖：
1. 命中：TTL 内第二次请求不穿透外部 API（mock 仅被调用一次），响应带 cached=True；
2. refresh=1：绕过缓存强制穿透；
3. 建分支成功后缓存失效（新分支立即可见）；
4. TTL 过期后重新穿透；
5. 失败（502）不入缓存。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from unittest.mock import patch
from fastapi.testclient import TestClient
from src.api.main import app
from src.auth import grape_account_db
from src.api.routes import scenes as scenes_mod

OWNER = "test542b"
REPO = "branches"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"


def _jwt_headers():
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    uid = grape_account_db.create_grape_account("test_542_branches", email=None, password="test123")
    if not uid:
        uid = grape_account_db.get_grape_account_by_username("test_542_branches")
    tok = create_access_token(uid["id"], uid["username"], get_token_version(uid["id"]))
    return {"Authorization": f"Bearer {tok}"}


client = TestClient(app)
_headers = None


def setup_module():
    global _headers
    _headers = _jwt_headers()


def _purge_cache():
    with scenes_mod._branches_cache_lock:
        scenes_mod._branches_cache.clear()


def _api_branches(name):
    return {"name": name, "protected": False}


class TestBranchesTTLCache:

    def setup_method(self):
        _purge_cache()

    def teardown_method(self):
        _purge_cache()

    def _get(self, qs=""):
        return client.get(f"/api/scenes/branches/{OWNER}/{REPO}?repo_key={REPO_KEY}{qs}",
                          headers=_headers)

    def test_cache_hit_within_ttl(self):
        calls = {"n": 0}

        def _fake_req(method, url, token, payload=None):
            calls["n"] += 1
            assert method == "GET"
            return [_api_branches("main"), _api_branches("dev")]

        with patch("src.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.user.repo_manager._req", side_effect=_fake_req):
            r1 = self._get()
            assert r1.status_code == 200, r1.text
            assert r1.json()["cached"] is False
            assert [b["name"] for b in r1.json()["branches"]] == ["main", "dev"]

            r2 = self._get()
            assert r2.status_code == 200
            assert r2.json()["cached"] is True
            assert [b["name"] for b in r2.json()["branches"]] == ["main", "dev"]

        assert calls["n"] == 1, "TTL 内第二次请求不应穿透外部 API"

    def test_refresh_bypasses_cache(self):
        calls = {"n": 0}

        def _fake_req(method, url, token, payload=None):
            calls["n"] += 1
            return [_api_branches("main")]

        with patch("src.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.user.repo_manager._req", side_effect=_fake_req):
            self._get()
            r2 = self._get("&refresh=1")
            assert r2.status_code == 200
            assert r2.json()["cached"] is False

        assert calls["n"] == 2, "refresh=1 应绕过缓存强制穿透"

    def test_create_branch_invalidates_cache(self):
        get_calls = {"n": 0}

        def _fake_get(method, url, token, payload=None):
            get_calls["n"] += 1
            return [_api_branches("main")]

        def _fake_post(method, url, token, payload=None):
            return {"name": payload["branch_name"]}

        with patch("src.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.user.repo_manager._req", side_effect=_fake_post):
            r_create = client.post(f"/api/scenes/branches/{OWNER}/{REPO}?repo_key={REPO_KEY}",
                                   json={"branch_name": "feat/new", "refs": "main"},
                                   headers=_headers)
            assert r_create.status_code == 200, r_create.text

        with patch("src.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.user.repo_manager._req", side_effect=_fake_get):
            r1 = self._get()
            assert r1.status_code == 200 and r1.json()["cached"] is False
            # 建分支后缓存已失效 → 再取应重新穿透（而非命中建分支前的旧缓存）
            r2 = self._get()
            assert r2.status_code == 200 and r2.json()["cached"] is True
        assert get_calls["n"] == 1, "建分支失效后只应穿透一次"

    def test_ttl_expiry_refetches(self):
        calls = {"n": 0}

        def _fake_req(method, url, token, payload=None):
            calls["n"] += 1
            return [_api_branches("main")]

        with patch("src.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.user.repo_manager._req", side_effect=_fake_req):
            self._get()
            # 把缓存时间戳拨回过去（超过 TTL 90s）
            key = scenes_mod._branches_cache_key("gitcode", OWNER, REPO)
            with scenes_mod._branches_cache_lock:
                _ts, _payload = scenes_mod._branches_cache[key]
                scenes_mod._branches_cache[key] = (_ts - scenes_mod._BRANCHES_CACHE_TTL - 1, _payload)
            r2 = self._get()
            assert r2.status_code == 200
            assert r2.json()["cached"] is False

        assert calls["n"] == 2, "TTL 过期后应重新穿透"

    def test_error_not_cached(self):
        def _raise(method, url, token, payload=None):
            raise RuntimeError("GitCode API failed (500)")

        with patch("src.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.user.repo_manager._req", side_effect=_raise):
            r1 = self._get()
            assert r1.status_code == 502
            # 失败不入缓存：下一次仍应尝试穿透（而非命中错误）
            r2 = self._get()
            assert r2.status_code == 502

        with scenes_mod._branches_cache_lock:
            assert not scenes_mod._branches_cache, "失败响应不应入缓存"
