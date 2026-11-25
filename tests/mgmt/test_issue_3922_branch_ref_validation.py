"""Issue #3922 回归测试：非法分支名前置 400 + 上游 4xx 逐档透传。

缺陷：`POST /api/scenes/branches/{owner}/{repo}` 只判空、不校验 ref 规则，
非法分支名直接打到上游；上游返回 400（输入非法）又被统一折叠成 502「托管服务不可用」，
且 `VcsApiClient` 错误 dict 里的 `detail`（说明原因）被丢弃 —— 用户既不知道违反哪条规则
也无法自助修正，同时客户端错误污染 5xx 告警口径。

覆盖：
1. `validate_branch_name()`：git check-ref-format + GitCode 约束的正/反例；
2. `map_upstream_error()`：4xx 透传（message 可读且脱敏）、409 冲突、5xx/无码 → 502；
3. 路由三路径：非法名 400 且不调用上游；上游 400 → 400；上游 500 → 502。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.main import app
from src.domains.mgmt.auth import grape_account_db
from src.domains.mgmt.routes import _vcs_error_map as em
from src.domains.mgmt.routes import scenes as scenes_mod
from src.kernel.git_ref import (
    MAX_BRANCH_NAME_LENGTH,
    is_valid_branch_name,
    validate_branch_name,
)

OWNER = "t3922"
REPO = "branches"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"

# 上游 400 报文：可读原因要透传（用户据此修正），内部 URL 与明文 token 必须剥离
UPSTREAM_REASON = "branch name contains an invalid character"
UPSTREAM_TOKEN = "tk_LEAKED_SECRET"
UPSTREAM_URL = (
    "https://gitcode.com/api/v5/repos/o/r/branches?access_token=" + UPSTREAM_TOKEN)
UPSTREAM_DETAIL = (
    '{"error_code":"BAD_REQUEST","error_message":"' + UPSTREAM_REASON
    + '","url":"' + UPSTREAM_URL + '"}'
)

client = TestClient(app)

_headers = None


def _jwt_headers():
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    uid = grape_account_db.create_grape_account(
        "test_3922_branches", email=None, password="test123")
    if not uid:
        uid = grape_account_db.get_grape_account_by_username("test_3922_branches")
    tok = create_access_token(uid["id"], uid["username"], get_token_version(uid["id"]))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _headers
    _headers = _jwt_headers()


def _post(branch, refs="main"):
    return client.post(
        f"/api/scenes/branches/{OWNER}/{REPO}?repo_key={REPO_KEY}",
        json={"branch_name": branch, "refs": refs}, headers=_headers)


def _assert_no_upstream_leak(text):
    """响应体不得回吐内部 URL/token（#383/#395/#3922 防泄露约定）。"""
    assert "https://" not in text, f"响应体泄露内部 URL: {text}"
    assert "gitcode.com" not in text, f"响应体泄露上游域名: {text}"
    assert UPSTREAM_TOKEN not in text, f"响应体泄露上游 token: {text}"


# === 1. ref 规则校验（纯函数） ===

INVALID_NAMES = [
    "-x",                     # 以 '-' 开头（会被命令行当作选项）
    "refs/heads/x",           # 完整 ref 形态（平台只要分支名）
    "refs/remotes/origin/x",  # 远程跟踪 ref 形态
    "a b",                    # 空格
    "a..b",                   # 连续两点
    "x.lock",                 # 以 .lock 结尾
    "a~b",                    # ~
    "a^b",                    # ^
    "a:b",                    # :
    "a?b",                    # ?
    "a*b",                    # *
    "a[b",                    # [
    "a\\b",                   # 反斜杠
    'a"b',                    # 双引号
    "a'b",                    # 单引号
    "@",                      # 单个 '@' 在 git 中有特殊语义
    "@{1}",                   # @{ 形态
    "a//b",                   # 连续斜杠
    "/a",                     # 以 '/' 开头
    "a/",                     # 以 '/' 结尾
    ".a",                     # 以 '.' 开头
    "a/.hidden",              # 分片以 '.' 开头
    "a.",                     # 以 '.' 结尾
    "a\x01b",                 # 控制字符
    "f" * (MAX_BRANCH_NAME_LENGTH + 1),  # 超长
]


class TestValidateBranchName:

    def test_invalid_names_rejected_with_reason(self):
        for name in INVALID_NAMES:
            reason = validate_branch_name(name)
            assert reason, f"非法分支名未被拒绝: {name!r}"
            assert isinstance(reason, str) and len(reason) > 3, reason
            assert not is_valid_branch_name(name)

    def test_valid_names_accepted(self):
        for name in ("main", "master", "feat/issue-3922", "release/v1.2.3",
                     "a-b_c.d", "x/y/z", "fix/3922-branch-ref"):
            assert validate_branch_name(name) == "", f"合法分支名被误拒: {name!r}"
            assert is_valid_branch_name(name)

    def test_reason_is_actionable(self):
        assert "'.lock'" in validate_branch_name("x.lock")
        assert "'-'" in validate_branch_name("-x")
        assert "refs/heads/" in validate_branch_name("refs/heads/x")
        assert "refs/remotes/" in validate_branch_name("refs/remotes/x")
        assert "'..'" in validate_branch_name("a..b")

    def test_empty_and_blank(self):
        for name in ("", "   ", None):
            assert validate_branch_name(name)


# === 2. 单点映射工具 map_upstream_error ===

class TestMapUpstreamError:

    def test_upstream_400_passes_through_with_readable_reason(self):
        exc = em.map_upstream_error(
            400, {"error": "HTTP 400", "status_code": 400, "detail": UPSTREAM_DETAIL},
            stage="scenes_branch_create")
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 400
        assert exc.detail["code"] == 400
        assert exc.detail["upstream"]["status"] == 400
        assert exc.detail["retryable"] is False
        assert UPSTREAM_REASON in exc.detail["message"], exc.detail["message"]
        assert "https://" not in str(exc.detail)
        assert UPSTREAM_TOKEN not in str(exc.detail)

    def test_bare_status_is_not_a_readable_reason(self):
        # 只有 "HTTP 400" 不含语义 → 走兜底文案而不是把状态码当原因
        exc = em.map_upstream_error(400, {"error": "HTTP 400"},
                                    stage="scenes_branch_create",
                                    message="创建分支被上游拒绝")
        assert exc.detail["message"] == "创建分支被上游拒绝"

    def test_upstream_error_message_masks_credentials_outside_url(self):
        msg = em.upstream_error_message(
            '{"error_message":"bad request: access_token=' + UPSTREAM_TOKEN + ' rejected"}')
        assert UPSTREAM_TOKEN not in msg, msg
        assert "access_token=***" in msg, msg

    def test_upstream_400_without_detail_uses_fallback_message(self):
        exc = em.map_upstream_error(400, "", stage="scenes_branch_create",
                                    message="创建分支被上游拒绝")
        assert exc.status_code == 400
        assert exc.detail["message"] == "创建分支被上游拒绝"

    def test_conflict_codes_map_to_409(self):
        for code in (409, 422):
            exc = em.map_upstream_error(
                code, {"status_code": code, "detail": "branch already exists"},
                conflict_on=(409, 422), stage="scenes_branch_create")
            assert exc.status_code == 409
            assert exc.detail["message"] == "分支已存在"

    def test_conflict_marker_beats_passthrough(self):
        exc = em.map_upstream_error(
            400, {"error": "HTTP 400", "detail": '{"message":"Branch already exists"}'},
            conflict_on=(409, 422), conflict_markers=("already exists",),
            stage="scenes_branch_create")
        assert exc.status_code == 409

    def test_upstream_5xx_maps_to_502(self):
        for code in (500, 502, 503):
            exc = em.map_upstream_error(code, {"status_code": code, "detail": UPSTREAM_DETAIL},
                                        stage="scenes_branch_create")
            assert exc.status_code == 502
            assert exc.detail["error_code"] == f"UPSTREAM_SCENES_BRANCH_CREATE_{code}"
            assert UPSTREAM_REASON not in str(exc.detail)

    def test_transport_error_maps_to_502(self):
        exc = em.map_upstream_error(0, TimeoutError(f"timed out {UPSTREAM_URL}"),
                                    stage="scenes_branch_create")
        assert exc.status_code == 502
        assert exc.detail["upstream"]["status"] is None
        assert "timed out" not in str(exc.detail)

    def test_resource_and_permission_semantics_unchanged(self):
        missing = em.map_upstream_error(404, {"status_code": 404, "detail": UPSTREAM_DETAIL},
                                        stage="scenes_branch_create")
        assert missing.status_code == 404
        assert missing.detail["message"] == "仓库不存在或无权访问"
        for code in (401, 403):
            denied = em.map_upstream_error(code, {"status_code": code, "detail": UPSTREAM_DETAIL},
                                           stage="scenes_branch_create")
            assert denied.status_code == 403
            assert denied.detail["error_code"] == "TOKEN_PERMISSION_INSUFFICIENT"

    def test_map_vcs_error_read_path_semantics_untouched(self):
        # #4169 契约：读路径无 conflict_on 时 409 仍折叠为 502（不得被 4xx 透传改动）
        assert em.map_vcs_error(
            {"error": "HTTP 409", "status_code": 409, "detail": "x"}).status_code == 502


# === 3. POST /api/scenes/branches/{owner}/{repo} ===

class TestCreateBranchRefPaths:

    def setup_method(self):
        with scenes_mod._branches_cache_lock:
            scenes_mod._branches_cache.clear()

    def teardown_method(self):
        with scenes_mod._branches_cache_lock:
            scenes_mod._branches_cache.clear()

    def test_invalid_ref_rejected_without_calling_upstream(self):
        for name in ("-x", "refs/heads/x", "a b", "a..b", "x.lock"):
            upstream = MagicMock(return_value={"name": name})
            with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh",
                       return_value="tok") as token_mock, \
                 patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_branch",
                       upstream):
                r = _post(name)
            assert r.status_code == 400, f"{name!r} 应 400，实际 {r.status_code}: {r.text}"
            detail = r.json()["detail"]
            assert detail["code"] == 400
            assert detail["error_code"] == "INVALID_BRANCH_NAME"
            assert detail["field"] == "branch_name"
            assert detail["message"], "400 必须给出可操作原因"
            assert upstream.call_count == 0, f"{name!r} 不应触达上游"
            assert token_mock.call_count == 0, f"{name!r} 不应触发凭据解析"

    def test_upstream_400_maps_to_400_with_sanitized_reason(self):
        with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_branch",
                   return_value={"error": "HTTP 400", "status_code": 400,
                                 "detail": UPSTREAM_DETAIL}):
            r = _post("feat/issue-3922")
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["code"] == 400
        assert UPSTREAM_REASON in detail["message"], "应透传可读原因"
        _assert_no_upstream_leak(r.text)

    def test_upstream_400_exception_form_maps_to_400(self):
        def _raise(*a, **k):
            raise RuntimeError(f"GitCode API POST {UPSTREAM_URL} failed (400): {UPSTREAM_DETAIL}")

        with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_branch",
                   side_effect=_raise):
            r = _post("feat/issue-3922")
        assert r.status_code == 400, r.text
        assert UPSTREAM_REASON in r.json()["detail"]["message"], r.text
        _assert_no_upstream_leak(r.text)

    def test_upstream_5xx_maps_to_502(self):
        with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_branch",
                   return_value={"error": "HTTP 500", "status_code": 500,
                                 "detail": UPSTREAM_DETAIL}):
            r = _post("feat/issue-3922")
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error_code"] == "UPSTREAM_SCENES_BRANCH_CREATE_500"
        _assert_no_upstream_leak(r.text)

    def test_valid_branch_still_created(self):
        with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="tok"), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_branch",
                   return_value={"name": "feat/issue-3922"}) as upstream:
            r = _post("feat/issue-3922")
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "feat/issue-3922"
        assert upstream.call_count == 1