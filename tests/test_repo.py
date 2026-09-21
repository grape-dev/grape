"""Repo 领域对象测试（Issue #200）。

覆盖：
- provider 解析（gitcode/github/无前缀）
- api_base 按 provider 分发
- clone_url 按 provider 分发
- bot_token 按 provider 分发（github→get_github_token, 其他→get_gitcode_token）
- bot_token 空 repo_key 处理
"""
import pytest


class TestRepoInit:
    def test_gitcode_repo_key(self):
        from src.repo import Repo
        r = Repo("gitcode:owner/repo")
        assert r.provider == "gitcode"
        assert r.owner == "owner"
        assert r.name == "repo"
        assert r.repo_key == "gitcode:owner/repo"

    def test_github_repo_key(self):
        from src.repo import Repo
        r = Repo("github:owner/repo")
        assert r.provider == "github"
        assert r.owner == "owner"
        assert r.name == "repo"

    def test_no_prefix_defaults_gitcode(self):
        from src.repo import Repo
        r = Repo("owner/repo")
        assert r.provider == "gitcode"
        assert r.owner == "owner"
        assert r.name == "repo"

    def test_empty_repo_key(self):
        from src.repo import Repo
        r = Repo("")
        assert r.provider == "gitcode"
        assert r.owner == ""
        assert r.name == ""


class TestRepoApiBase:
    def test_gitcode_api_base(self):
        from src.repo import Repo
        assert Repo("gitcode:owner/repo").api_base == "https://gitcode.com/api/v5"

    def test_github_api_base(self):
        from src.repo import Repo
        assert Repo("github:owner/repo").api_base == "https://api.github.com"

    def test_unknown_provider_falls_back_gitcode(self):
        from src.repo import Repo
        assert Repo("gitlab:owner/repo").api_base == "https://gitcode.com/api/v5"


class TestRepoCloneUrl:
    def test_gitcode_clone_url(self):
        from src.repo import Repo
        assert Repo("gitcode:owner/repo").clone_url == "https://gitcode.com/owner/repo.git"

    def test_github_clone_url(self):
        from src.repo import Repo
        assert Repo("github:owner/repo").clone_url == "https://github.com/owner/repo.git"


class TestRepoBotToken:
    def test_github_calls_get_github_token(self, monkeypatch):
        from src.repo import Repo
        called = {"fn": ""}
        def _fake_github_token(repo_key=""):
            called["fn"] = "github"
            called["repo_key"] = repo_key
            return "gh-token-123"
        def _fake_gitcode_token(repo_key="", user_id=0):
            called["fn"] = "gitcode"
            return "gc-token-456"
        monkeypatch.setattr("src.user.db.get_github_token", _fake_github_token)
        monkeypatch.setattr("src.user.db.get_gitcode_token", _fake_gitcode_token)
        token = Repo("github:owner/repo").bot_token
        assert token == "gh-token-123"
        assert called["fn"] == "github"
        assert called["repo_key"] == "github:owner/repo"

    def test_gitcode_calls_get_gitcode_token(self, monkeypatch):
        from src.repo import Repo
        called = {"fn": ""}
        def _fake_github_token(repo_key=""):
            called["fn"] = "github"
            return "gh-token-123"
        def _fake_gitcode_token(repo_key="", user_id=0):
            called["fn"] = "gitcode"
            called["repo_key"] = repo_key
            return "gc-token-456"
        monkeypatch.setattr("src.user.db.get_github_token", _fake_github_token)
        monkeypatch.setattr("src.user.db.get_gitcode_token", _fake_gitcode_token)
        token = Repo("gitcode:owner/repo").bot_token
        assert token == "gc-token-456"
        assert called["fn"] == "gitcode"
        assert called["repo_key"] == "gitcode:owner/repo"

    def test_no_prefix_calls_get_gitcode_token(self, monkeypatch):
        from src.repo import Repo
        called = {"fn": ""}
        def _fake_gitcode_token(repo_key="", user_id=0):
            called["fn"] = "gitcode"
            return "gc-token"
        monkeypatch.setattr("src.user.db.get_gitcode_token", _fake_gitcode_token)
        token = Repo("owner/repo").bot_token
        assert token == "gc-token"
        assert called["fn"] == "gitcode"

    def test_empty_token_returns_empty_string(self, monkeypatch):
        from src.repo import Repo
        monkeypatch.setattr("src.user.db.get_gitcode_token", lambda repo_key="", user_id=0: "")
        assert Repo("gitcode:owner/repo").bot_token == ""

    def test_none_token_returns_empty_string(self, monkeypatch):
        from src.repo import Repo
        monkeypatch.setattr("src.user.db.get_gitcode_token", lambda repo_key="", user_id=0: None)
        assert Repo("gitcode:owner/repo").bot_token == ""
