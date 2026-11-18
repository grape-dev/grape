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

    def test_gitlab_api_base(self):
        # 自建 GitLab：实例地址来自配置，仅断言 API v4 后缀
        from src.repo import Repo
        assert Repo("gitlab:owner/repo").api_base.endswith("/api/v4")

    def test_unknown_provider_falls_back_gitcode(self):
        from src.repo import Repo
        assert Repo("unknown:owner/repo").api_base == "https://gitcode.com/api/v5"


class TestRepoCloneUrl:
    def test_gitcode_clone_url(self):
        from src.repo import Repo
        assert Repo("gitcode:owner/repo").clone_url == "https://gitcode.com/owner/repo.git"

    def test_github_clone_url(self):
        from src.repo import Repo
        assert Repo("github:owner/repo").clone_url == "https://github.com/owner/repo.git"


class TestRepoBotToken:
    """Repo.bot_token 委托 token_service → TokenResolver（按 repo_key 前缀选 provider）。"""

    @staticmethod
    def _stub_resolver(monkeypatch, token="tok"):
        from src.token_resolver import TokenResolver
        seen = {}

        def _fake_get_token(self, repo_key):
            seen["provider"] = self.provider.provider_name
            seen["repo_key"] = repo_key
            return token

        monkeypatch.setattr(TokenResolver, "get_token", _fake_get_token)
        return seen

    def test_github_provider(self, monkeypatch):
        from src.repo import Repo
        seen = self._stub_resolver(monkeypatch, "gh-token-123")
        assert Repo("github:owner/repo").bot_token == "gh-token-123"
        assert seen["provider"] == "github"
        assert seen["repo_key"] == "github:owner/repo"

    def test_gitlab_provider(self, monkeypatch):
        from src.repo import Repo
        seen = self._stub_resolver(monkeypatch, "gl-token-789")
        assert Repo("gitlab:group/sub/repo").bot_token == "gl-token-789"
        assert seen["provider"] == "gitlab"

    def test_gitcode_provider(self, monkeypatch):
        from src.repo import Repo
        seen = self._stub_resolver(monkeypatch, "gc-token-456")
        assert Repo("gitcode:owner/repo").bot_token == "gc-token-456"
        assert seen["provider"] == "gitcode"
        assert seen["repo_key"] == "gitcode:owner/repo"

    def test_no_prefix_defaults_gitcode(self, monkeypatch):
        from src.repo import Repo
        seen = self._stub_resolver(monkeypatch, "gc-token")
        assert Repo("owner/repo").bot_token == "gc-token"
        assert seen["provider"] == "gitcode"

    def test_empty_token_returns_empty_string(self, monkeypatch):
        from src.repo import Repo
        self._stub_resolver(monkeypatch, "")
        assert Repo("gitcode:owner/repo").bot_token == ""

    def test_none_token_returns_empty_string(self, monkeypatch):
        from src.repo import Repo
        self._stub_resolver(monkeypatch, None)
        assert not Repo("gitcode:owner/repo").bot_token
