"""通道能力位（checks / statuses / PR↔Issue 关联 / gitlab 子路径）验收用例。

覆盖上一轮 clone 收口（commit 3ec48ae53）之外的第二刀：
  · checks/status 通道由平台模块能力位决定，不再 `if p == "github" else gitcode`
  · gitee 用原生 /commits/{ref}/check-runs（平台 swagger 实测存在），不再错打 gitcode Actions
  · gitee / gitlab 无 combined status → 显式 404 statuses_unavailable，且**零上游调用**
  · PR↔Issue 关联解除按 PR_ISSUE_LINK_NATIVE 分派（不再比较平台名）
  · gitlab 的评论/MR 文件子路径由模块 rewrite_path 收口（/comments→/notes、/files→/changes）

本机无 DB：平台卡用内存卡替换，上游调用用假 _vcs_req 捕获（只看 URL 与 provider）。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_CARDS = {
    "gitcode": {"host": "gitcode.com", "clone_auth": "x_access_token", "bot_username": ""},
    "github": {"host": "github.com", "clone_auth": "x_access_token", "bot_username": ""},
    "gitlab": {"host": "gitlab.com", "clone_auth": "oauth2", "bot_username": ""},
    "gitee": {"host": "gitee.com", "clone_auth": "basic_password", "bot_username": ""},
}

USER = {"id": 1, "username": "probe", "agent_type": "coding", "auth_type": "session_token"}


@pytest.fixture()
def api(monkeypatch):
    """import 代理模块 + 替换平台卡 + 捕获上游调用。"""
    import src.api.git_repo_api as mod
    import src.domains.runtime.vcs.platform_card as pc

    def _get(provider):
        cfg = _CARDS.get(provider)
        if cfg is None:
            return None
        card = types.SimpleNamespace(**cfg)
        card.token_env_name = f"BOT_{provider.upper()}_TOKEN"
        return card

    monkeypatch.setattr(pc, "get_platform_card", _get)
    calls = []
    monkeypatch.setattr(mod, "_repo_token", lambda o, r, u, p="": "fake-token")
    monkeypatch.setattr(mod, "_vcs_req",
                        lambda provider, method, url, token, body=None: (
                            calls.append((provider, method, url)) or
                            ({"sha": "0" * 40, "total_count": 0, "check_runs": [],
                              "issues": [], "id": 1} if method == "GET" else {"id": 1})))
    mod._calls = calls
    return mod


# ------------------------------------------------------------------ checks
def test_checks_gitee_uses_gitee_native_endpoint(api):
    api.list_check_runs("foo", "bar", "main", user=USER, p="gitee")
    assert api._calls, "必须发生上游调用"
    provider, method, url = api._calls[-1]
    assert provider == "gitee"
    assert url == "https://gitee.com/api/v5/repos/foo/bar/commits/main/check-runs"


def test_checks_gitlab_is_explicitly_unavailable(api):
    with pytest.raises(HTTPException) as ei:
        api.list_check_runs("foo", "bar", "main", user=USER, p="gitlab")
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == "checks_unavailable"
    assert api._calls == [], "无通道平台不得发起任何上游调用（尤其不得打 gitcode）"


def test_checks_gitcode_still_goes_through_actions(api):
    api.list_check_runs("foo", "bar", "main", user=USER, p="gitcode")
    assert any("api.gitcode.com/api/v8" in u for _, _, u in api._calls)


def test_checks_github_unchanged(api):
    api.list_check_runs("foo", "bar", "main", user=USER, p="github")
    assert api._calls[-1][2] == "https://api.github.com/repos/foo/bar/commits/main/check-runs"


# ------------------------------------------------------------------ statuses
@pytest.mark.parametrize("provider", ["gitee", "gitlab"])
def test_statuses_without_channel_are_404_and_never_call_upstream(api, provider):
    with pytest.raises(HTTPException) as ei:
        api.list_commit_statuses("foo", "bar", "main", user=USER, p=provider)
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == "statuses_unavailable"
    assert api._calls == [], f"{provider} 无 status 通道，不得代打别的平台"


def test_statuses_github_unchanged(api):
    api.list_commit_statuses("foo", "bar", "main", user=USER, p="github")
    assert api._calls[-1][2] == "https://api.github.com/repos/foo/bar/commits/main/status"


def test_statuses_gitcode_resolves_sha_on_same_provider(api):
    api.list_commit_statuses("foo", "bar", "main", user=USER, p="gitcode")
    urls = [u for _, _, u in api._calls]
    assert any(u.startswith("https://gitcode.com/") and "/commits/" in u and u.endswith("/status")
               for u in urls), urls
    assert all("github.com" not in u and "gitee.com" not in u for u in urls)


# ------------------------------------------------------------------ unlink
def test_unlink_gitee_is_idempotent_noop(api):
    out = api.unlink_pull_issue("foo", "bar", 1, 2, user=USER, p="gitee")
    assert out["unlinked"] is False
    assert "PR_ISSUE_LINK_NATIVE=false" in out["reason"]
    assert api._calls == []


def test_unlink_gitcode_still_writes(api):
    api.unlink_pull_issue("foo", "bar", 1, 2, user=USER, p="gitcode")
    assert any(m == "GET" for _, m, _ in api._calls)


# ------------------------------------------------------------------ gitlab 子路径
def test_gitlab_issue_comments_map_to_notes(api):
    api.list_issue_comments("foo", "bar", 3, user=USER, p="gitlab")
    assert api._calls[-1][2] == "https://gitlab.com/api/v4/projects/foo%2Fbar/issues/3/notes"


def test_gitlab_mr_comments_map_to_notes(api):
    api.list_pull_comments("foo", "bar", 7, user=USER, p="gitlab")
    assert api._calls[-1][2] == "https://gitlab.com/api/v4/projects/foo%2Fbar/merge_requests/7/notes"


def test_gitlab_mr_files_map_to_changes(api):
    api.list_pull_files("foo", "bar", 7, page=1, per_page=100, user=USER, p="gitlab")
    assert api._calls[-1][2] == (
        "https://gitlab.com/api/v4/projects/foo%2Fbar/merge_requests/7/changes?page=1&per_page=100")


def test_gitlab_comment_update_keeps_mr_number_in_path(api):
    api.update_pull_comment("foo", "bar", 7, "123", api.CommentBody(body="x"), user=USER, p="gitlab")
    assert api._calls[-1][2] == (
        "https://gitlab.com/api/v4/projects/foo%2Fbar/merge_requests/7/notes/123")


def test_gitee_comment_update_keeps_flat_path(api):
    api.update_pull_comment("foo", "bar", 7, "123", api.CommentBody(body="x"), user=USER, p="gitee")
    assert api._calls[-1][2] == "https://gitee.com/api/v5/repos/foo/bar/pulls/comments/123"