"""仓库领域对象：封装仓库维度的行为和数据。

与 repo_key.py 的区别：
- repo_key.py：纯标识转换工具（to_repo_key / parse_repo_key / split_owner_repo）
- repo.py：领域对象，封装业务行为（bot_token / api_base / clone_url）
"""

from src.repo_key import parse_repo_key

_HOSTS = {"gitcode": "gitcode.com", "github": "github.com"}
_API_BASES = {"gitcode": "https://gitcode.com/api/v5", "github": "https://api.github.com"}


class Repo:
    """仓库领域对象。"""

    def __init__(self, repo_key: str):
        self.repo_key = repo_key
        self.provider, self.owner, self.name = parse_repo_key(repo_key)

    @property
    def api_base(self) -> str:
        """返回该仓库对应平台的 API 基础 URL。"""
        return _API_BASES.get(self.provider, _API_BASES["gitcode"])

    @property
    def clone_url(self) -> str:
        """返回仓库的 git clone URL。"""
        host = _HOSTS.get(self.provider, _HOSTS["gitcode"])
        return f"https://{host}/{self.owner}/{self.name}.git"

    @property
    def bot_token(self) -> str:
        """按 provider 分发获取仓库 bot token（含三级兜底）。

        github → get_github_token（managed_repos.bot_github_token → user_accounts → 全局）
        其他   → get_gitcode_token（devops_agent_session → managed_repos.bot_gitcode_token → 全局）
        """
        if self.provider == "github":
            from src.user.db import get_github_token
            return get_github_token(repo_key=self.repo_key) or ""
        else:
            from src.user.db import get_gitcode_token
            return get_gitcode_token(repo_key=self.repo_key) or ""
