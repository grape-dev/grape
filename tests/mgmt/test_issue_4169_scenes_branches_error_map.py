"""Issue #4169 回归测试：/api/scenes/branches 上游错误映射（GET/POST）。

缺陷：#383/#395 的「上游 404/401/403 不折叠成 502」只修了 repos.py；
scenes 分支端点统一 `except Exception → 502` 并回吐上游原文，导致
「仓库不存在/无权访问」与「代码托管平台网关故障」不可区分且泄露上游报文。

覆盖：
1. 共享模块 `_vcs_error_map`：状态码字段优先 + `failed (404)` / `HTTP 404` 双正则兜底；
2. GET × {404, 401, 403, timeout, 500}：状态码/文案/无上游原文；
3. POST × {404, 401, 403, timeout, 500} + 409/422 仍 409；
4. 失败响应不入分支缓存。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.main import app
from src.domains.mgmt.auth import grape_account_db
from src.domains.mgmt.routes import _vcs_error_map as em
from src.domains.mgmt.routes import scenes as scenes_mod

OWNER = "t4169"
REPO = "branches"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"

UPSTREAM_SECRET = "Project not found"
UPSTREAM_URL = "https://gitcode.com/api/v5/repos/o/r"
UPSTREAM_BODY = (
    '{"error_code":"REPO_NOT_FOUND","error_message":"'
    + UPSTREAM_SECRET + '","url":"' + UPSTREAM_URL + '"}'
)

client = TestClient(app)

_headers = None


def _jwt_headers():
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    uid = grape_account_db.create_grape_account(
        "test_4169_branches", email=None, password="test123")
    if not uid:
        uid = grape_account_db.get_grape_account_by_username("test_4169_branches")
    tok = create_access_token(uid["id"], uid["username"], get_token_version(uid["id"]))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _headers
    _headers = _jwt_headers()


def _purge_cache():
    with scenes_mod._branches_cache_lock:
        scenes_mod._branches_cache.clear()


def _get(qs=""):
    return client.get(
        f"/api/scenes/branches/{OWNER}/{REPO}?repo_key={REPO_KEY}{qs}",
        headers=_headers)


def _post(branch="feat/new"):
    return client.post(
        f"/api/scenes/branches/{OWNER}/{REPO}?repo_key={REPO_KEY}",
        json={"branch_name": branch, "refs": "main"}, headers=_headers)


def _assert_no_upstream_leak(text):
    """响应体不得回吐上游原文/内部 URL（#383/#395 防泄露约定）。"""
    assert UPSTREAM_SECRET not in text, f"响应体泄露上游报文: {text}"
    assert UPSTREAM_URL not in text, f"响应体泄露上游 URL: {text}"
    assert "gitcode.com" not in text, f"响应体泄露上游域名: {text}"


# === 1. 共享模块：状态码提取 ===


class TestUpstreamStatusCode:

    def test_dict_status_code_field_wins(self):
        assert em.upstream_status_code(
            {"error": "HTTP 404", "status_code": 404, "detail": UPSTREAM_BODY}) == 404

    def test_dict_http_404_text_fallback(self):
        assert em.upstream_status_code({"error": "HTTP 403"}) == 403

    def test_dict_without_code_is_not_http_error(self):
        assert em.upstream_status_code({"error": "timeout (25s)"}) == 0

    def test_exception_failed_form(self):
        assert em.upstream_status_code(
            RuntimeError(f"GitCode API GET {UPSTREAM_URL} failed (404): {UPSTREAM_BODY}")) == 404

    def test_exception_http_form(self):
        assert em.upstream_status_code(RuntimeError(f"HTTP 500 {UPSTREAM_BODY}")) == 500

    def test_unknown_error_returns_zero(self):
        assert em.upstream_status_code(RuntimeError("random network glitch")) == 0

    def test_detail_body_number_is_not_treated_as_status(self):
        # detail 是上游全文：其中的数字不得被误判为状态码
        assert em.upstream_status_code(
            {"error": "HTTP 502", "detail": "chunk 404 of 5"}) == 502


# === 2. 共享模块：映射结果 ===


class TestMapVcsError:

    def test_404_maps_to_404_business_message(self):
        exc = em.map_vcs_error({"error": "HTTP 404", "status_code": 404,
                                "detail": UPSTREAM_BODY})
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 404
        assert exc.detail["code"] == 404
        assert exc.detail["message"] == "仓库不存在或无权访问"
        assert UPSTREAM_SECRET not in str(exc.detail)

    def test_401_and_403_map_to_token_permission_insufficient(self):
        for code in (401, 403):
            exc = em.map_vcs_error({"error": f"HTTP {code}", "status_code": code,
                                    "detail": UPSTREAM_BODY})
            assert exc.status_code == 403
            assert exc.detail["error_code"] == "TOKEN_PERMISSION_INSUFFICIENT"

    def test_timeout_maps_to_502_without_upstream_text(self):
        exc = em.map_vcs_error({"error": "timeout (25s)"}, stage="scenes_branches")
        assert exc.status_code == 502
        assert exc.detail["code"] == 502
        assert exc.detail["error_code"] == "UPSTREAM_SCENES_BRANCHES"
        assert exc.detail["retryable"] is True
        assert "timeout" not in str(exc.detail)

    def test_5xx_maps_to_502_with_status_suffix(self):
        exc = em.map_vcs_error(RuntimeError(f"HTTP 503 {UPSTREAM_BODY}"),
                               stage="scenes_branches")
        assert exc.status_code == 502
        assert exc.detail["error_code"] == "UPSTREAM_SCENES_BRANCHES_503"
        assert exc.detail["upstream"]["status"] == 503
        assert UPSTREAM_URL not in str(exc.detail)

    def test_conflict_only_when_requested(self):
        err = {"error": "HTTP 409", "status_code": 409, "detail": "branch already exists"}
        assert em.map_vcs_error(err).status_code == 502
        assert em.map_vcs_error(err, conflict_on=(409, 422)).status_code == 409

    def test_conflict_markers_fallback(self):
        exc = em.map_vcs_error({"error": "HTTP 400", "detail": "Branch already exists"},
                               conflict_markers=("already exists",))
        assert exc.status_code == 409
        assert exc.detail["message"] == "分支已存在"


# === 3. GET /api/scenes/branches/{owner}/{repo} ===


class TestGetBranchesUpstreamErrors:

    def setup_method(self):
        _purge_cache()

    def teardown_method(self):
        _purge_cache()

    def _get_with(self, side_effect):
        with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient._card_or_legacy_get",
                   side_effect=side_effect):
            return _get()

    def test_upstream_404_maps_to_404(self):
        r = self._get_with(lambda *a, **k: {"error": "HTTP 404", "status_code": 404,
                                            "detail": UPSTREAM_BODY})
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == 404
        assert r.json()["detail"]["message"] == "仓库不存在或无权访问"
        _assert_no_upstream_leak(r.text)

    def test_upstream_401_maps_to_403(self):
        r = self._get_with(lambda *a, **k: {"error": "HTTP 401", "status_code": 401,
                                            "detail": UPSTREAM_BODY})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error_code"] == "TOKEN_PERMISSION_INSUFFICIENT"
        _assert_no_upstream_leak(r.text)

    def test_upstream_403_maps_to_403(self):
        r = self._get_with(lambda *a, **k: {"error": "HTTP 403", "status_code": 403,
                                            "detail": UPSTREAM_BODY})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error_code"] == "TOKEN_PERMISSION_INSUFFICIENT"
        _assert_no_upstream_leak(r.text)

    def test_timeout_maps_to_502_without_upstream_text(self):
        def _raise(*a, **k):
            raise TimeoutError(f"timed out reading {UPSTREAM_URL}")

        r = self._get_with(_raise)
        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert detail["code"] == 502
        assert detail["error_code"].startswith("UPSTREAM_SCENES_BRANCHES")
        assert "timed out" not in r.text
        assert UPSTREAM_URL not in r.text

    def test_upstream_5xx_maps_to_502_without_upstream_text(self):
        r = self._get_with(lambda *a, **k: {"error": "HTTP 500", "status_code": 500,
                                            "detail": UPSTREAM_BODY})
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error_code"] == "UPSTREAM_SCENES_BRANCHES_500"
        _assert_no_upstream_leak(r.text)

    def test_error_not_cached(self):
        with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient._card_or_legacy_get",
                   side_effect=lambda *a, **k: {"error": "HTTP 404", "status_code": 404,
                                                "detail": UPSTREAM_BODY}):
            assert _get().status_code == 404
            assert _get().status_code == 404
        with scenes_mod._branches_cache_lock:
            assert not scenes_mod._branches_cache, "失败响应不应入分支缓存"


# === 4. POST /api/scenes/branches/{owner}/{repo} ===


class TestCreateBranchUpstreamErrors:

    def setup_method(self):
        _purge_cache()

    def teardown_method(self):
        _purge_cache()

    def _post_with(self, side_effect):
        with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_branch",
                   side_effect=side_effect):
            return _post()

    def test_upstream_404_maps_to_404(self):
        r = self._post_with(lambda *a, **k: {"error": "HTTP 404", "status_code": 404,
                                             "detail": UPSTREAM_BODY})
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["message"] == "仓库不存在或无权访问"
        _assert_no_upstream_leak(r.text)

    def test_exception_404_form_maps_to_404(self):
        def _raise(*a, **k):
            raise RuntimeError(f"GitCode API POST {UPSTREAM_URL} failed (404): {UPSTREAM_BODY}")

        r = self._post_with(_raise)
        assert r.status_code == 404, r.text
        _assert_no_upstream_leak(r.text)

    def test_upstream_401_and_403_map_to_token_permission(self):
        for code in (401, 403):
            def _err(*a, _c=code, **k):
                return {"error": f"HTTP {_c}", "status_code": _c, "detail": UPSTREAM_BODY}

            r = self._post_with(_err)
            assert r.status_code == 403, r.text
            assert r.json()["detail"]["error_code"] == "TOKEN_PERMISSION_INSUFFICIENT"
            _assert_no_upstream_leak(r.text)

    def test_conflict_still_409(self):
        for code in (409, 422):
            def _err(*a, _c=code, **k):
                return {"error": f"HTTP {_c}", "status_code": _c, "detail": UPSTREAM_BODY}

            r = self._post_with(_err)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["message"] == "分支已存在"
        _assert_no_upstream_leak(r.text)

    def test_conflict_marker_without_status_code_still_409(self):
        r = self._post_with(lambda *a, **k: {"error": "HTTP 400",
                                             "detail": '{"message":"Branch already exists"}'})
        assert r.status_code == 409, r.text

    def test_timeout_maps_to_502_without_upstream_text(self):
        def _raise(*a, **k):
            raise TimeoutError(f"timed out posting {UPSTREAM_URL}")

        r = self._post_with(_raise)
        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert detail["error_code"].startswith("UPSTREAM_SCENES_BRANCH_CREATE")
        assert "timed out" not in r.text
        assert UPSTREAM_URL not in r.text

    def test_upstream_5xx_maps_to_502_without_upstream_text(self):
        r = self._post_with(lambda *a, **k: {"error": "HTTP 502", "status_code": 502,
                                             "detail": UPSTREAM_BODY})
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error_code"] == "UPSTREAM_SCENES_BRANCH_CREATE_502"
        _assert_no_upstream_leak(r.text)

    def test_failure_does_not_invalidate_or_write_cache(self):
        with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_branch",
                   side_effect=lambda *a, **k: {"error": "HTTP 404", "status_code": 404,
                                                "detail": UPSTREAM_BODY}):
            assert _post().status_code == 404
        with scenes_mod._branches_cache_lock:
            assert not scenes_mod._branches_cache, "建分支失败不应写缓存"