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
        """委托 service 层获取 token（R20.11）。

        R20.11.1: 改为调 src/token_service.py 的 get_repo_bot_token()，
        内部委托 TokenResolver.get_token()——所有调 Repo.bot_token 的场景
        自动继承短路/过期检测。

        R20.11.5: 不直接 import TokenResolver，避免循环依赖。
        签名不变（str → str），向后兼容。
        """
        from src.token_service import get_repo_bot_token
        return get_repo_bot_token(self.repo_key)
