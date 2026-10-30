"""git-repo tree 分支名含斜杠 404 修复测试（git-repo-tree-slash-ref-design §六）。

背景（grape-skills#95）：`grape git-repo tree --ref feat/issue-11` 返回 404。
根因：`get_repo_tree` 把 ref 裸拼进 URL 路径段 `/git/trees/{ref}`，含 `/` 的分支名被
GitCode 当路径分隔符拆成两段；而 contents 走 query 参数（urlencode 后 %2F）正常。

本文件守住：
- 含 `/` 的 ref：先解析 tree/commit sha → 再调 `git/trees/{sha}`（sha 无斜杠）；
- 无斜杠 ref / 空 ref / recursive 拒绝：保持现状零回归；
- commits 解析失败 → 保序降级返回原 ref（不新增 502）；
- GitHub：走 `_github_tree_sha` 既有语义（commit.tree.sha，无斜杠）。
"""
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key

UID = 98702
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_456"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = \
        lambda: {"id": UID, "username": "git_tester", "auth_type": "session_token",
                 "agent_type": "vod", "user_id": 0, "session_key": "s1",
                 "repo_key": f"gitcode:{OWNER}/{REPO}"}
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)
    monkeypatch.setattr("src.user.db.get_github_token", lambda *a, **k: BOT)


def _override_user(repo_key: str):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": repo_key}


def _routed_req_mock(recorded, route):
    """按 URL 子串路由的原生 API mock：route 返回对应响应，否则回退 echo。"""
    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "token": token, "body": body})
        for key, resp in route.items():
            if key in url:
                return resp
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}
    return _fake_req


def _commit_with_tree(sha, tree_sha):
    return {"sha": sha, "commit": {"tree": {"sha": tree_sha}, "message": "m"}}


class TestGitCodeTreeSlashRef:
    def test_slash_ref_resolves_tree_sha_first(self, monkeypatch):
        """QA-01：GitCode + ref=feat/issue-11 → 先 commits 解析 → git/trees/{sha}。"""
        recorded = []
        tree_sha = "abc123def456"
        monkeypatch.setattr(
            "src.user.repo_manager._req",
            _routed_req_mock(recorded, {
                "/commits": _commit_with_tree("c111222333", tree_sha),
            }))
        r = TestClient(app).get(f"/api/git-repo/{OWNER}/{REPO}/tree?ref=feat/issue-11")
        assert r.status_code == 200
        commits = [c for c in recorded if "/commits" in c["url"] and "sha=feat" in c["url"]]
        assert commits, "含斜杠 ref 必须先经 commits 解析（query 形态）"
        assert "sha=feat%2Fissue-11" in commits[-1]["url"], "ref 必须 urlencode，不裸插路径段"
        trees = [c for c in recorded if "/git/trees/" in c["url"]]
        assert trees and trees[-1]["url"].endswith(f"/git/trees/{tree_sha}"), \
            "取树必须用解析出的无斜杠 sha"

    def test_non_slash_ref_direct_no_extra_call(self, monkeypatch):
        """QA-02：GitCode + ref=main（无斜杠）→ 直连 git/trees/main，不解析。"""
        recorded = []
        monkeypatch.setattr("src.user.repo_manager._req", _routed_req_mock(recorded, {}))
        r = TestClient(app).get(f"/api/git-repo/{OWNER}/{REPO}/tree?ref=main")
        assert r.status_code == 200
        assert recorded[-1]["url"].endswith("/git/trees/main")
        assert not any("/commits" in c["url"] for c in recorded), "无斜杠 ref 不得多一次请求"

    def test_empty_ref_resolves_default(self, monkeypatch):
        """QA-03：GitCode + ref 空 → 沿用默认分支解析。"""
        recorded = []
        monkeypatch.setattr(
            "src.user.repo_manager._req",
            _routed_req_mock(recorded, {
                "/branches": [{"name": "main"}],
            }))
        r = TestClient(app).get(f"/api/git-repo/{OWNER}/{REPO}/tree")
        assert r.status_code == 200
        assert recorded[-1]["url"].endswith("/git/trees/main")

    def test_commits_parse_failure_degrades_to_raw_ref(self, monkeypatch):
        """QA-04：commits 解析失败（坏结构）→ 降级返回原 ref，不 502。"""
        recorded = []
        monkeypatch.setattr(
            "src.user.repo_manager._req",
            _routed_req_mock(recorded, {
                "/commits": {"unexpected": True},  # 非 list 且无 tree → 解析失败
            }))
        r = TestClient(app).get(f"/api/git-repo/{OWNER}/{REPO}/tree?ref=feat/issue-11")
        assert r.status_code == 200
        assert recorded[-1]["url"].endswith("/git/trees/feat/issue-11"), \
            "解析失败应保序降级原样透传"

    def test_recursive_gitcode_rejected(self, monkeypatch):
        """QA-05：GitCode + recursive → 400 拒绝（不回归）。"""
        monkeypatch.setattr("src.user.repo_manager._req", _routed_req_mock([], {}))
        r = TestClient(app).get(f"/api/git-repo/{OWNER}/{REPO}/tree?ref=feat/issue-11&depth=recursive")
        assert r.status_code == 400
        assert "暂不支持递归" in r.json()["detail"]["message"]


class TestGitHubTreeSlashRef:
    def test_github_slash_ref_uses_commit_tree_sha(self, monkeypatch):
        """QA-06：GitHub + ref 含 `/` → _github_tree_sha 解析 tree sha。"""
        _override_user(f"github:{OWNER}/{REPO}")
        recorded = []
        tree_sha = "gh_tree_9a8b7c"
        monkeypatch.setattr(
            "src.user.repo_manager._github_req",
            _routed_req_mock(recorded, {
                "/commits/": _commit_with_tree("ghc111", tree_sha),
            }))
        r = TestClient(app).get(f"/api/git-repo/{OWNER}/{REPO}/tree?ref=feat/issue-11")
        assert r.status_code == 200
        commits = [c for c in recorded if "/commits/" in c["url"]]
        assert commits, "GitHub 含斜杠 ref 走 commits 解析"
        assert recorded[-1]["url"].endswith(f"/git/trees/{tree_sha}")

    def test_github_non_slash_ref_keeps_behavior(self, monkeypatch):
        """QA-07：GitHub + ref 无 `/` → 沿现状直连 git/trees/{ref}（不解析）。"""
        _override_user(f"github:{OWNER}/{REPO}")
        recorded = []
        monkeypatch.setattr(
            "src.user.repo_manager._github_req",
            _routed_req_mock(recorded, {
                "/commits/": _commit_with_tree("ghc222", "gh_tree_5566"),
            }))
        r = TestClient(app).get(f"/api/git-repo/{OWNER}/{REPO}/tree?ref=main")
        assert r.status_code == 200
        assert recorded[-1]["url"].endswith("/git/trees/main"), \
            "无斜杠 ref 保持直连，不触发解析"

    def test_github_recursive_slash_ref_resolves(self, monkeypatch):
        """GitHub recursive + 含斜杠 ref → git/trees/{sha}?recursive=1。"""
        _override_user(f"github:{OWNER}/{REPO}")
        recorded = []
        tree_sha = "gh_tree_recur"
        monkeypatch.setattr(
            "src.user.repo_manager._github_req",
            _routed_req_mock(recorded, {
                "/commits/": _commit_with_tree("ghc333", tree_sha),
            }))
        r = TestClient(app).get(f"/api/git-repo/{OWNER}/{REPO}/tree?ref=feat/issue-11&depth=recursive")
        assert r.status_code == 200
        assert f"git/trees/{tree_sha}?" in recorded[-1]["url"] or \
            recorded[-1]["url"].endswith(f"git/trees/{tree_sha}")
        assert "recursive" in recorded[-1]["url"]