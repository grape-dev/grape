"""仓库领域对象：封装仓库维度的行为和数据。

与 repo_key.py 的区别：
- repo_key.py：纯标识转换工具（to_repo_key / parse_repo_key / split_owner_repo）
- repo.py：领域对象，封装业务行为（bot_token / api_base / clone_url）
"""

from src.kernel.repo_key import parse_repo_key

# 存量二元映射降级为无平台卡时的回退表（新平台一律卡驱动）
_HOSTS = {"gitcode": "gitcode.com", "github": "github.com"}
_API_BASES = {"gitcode": "https://gitcode.com/api/v5", "github": "https://api.github.com"}


def _card_for(provider: str):
    """平台卡优先（回退 runtime/repogit.py 写法）；查卡失败按无卡处理。"""
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        return get_platform_card(provider)
    except Exception:
        return None


class Repo:
    """仓库领域对象。"""

    def __init__(self, repo_key: str):
        self.repo_key = repo_key
        self.provider, self.owner, self.name = parse_repo_key(repo_key)

    @property
    def api_base(self) -> str:
        """返回该仓库对应平台的 API 基础 URL（平台卡优先，无卡回退存量表）。"""
        _card = _card_for(self.provider)
        if _card is not None and _card.api_base:
            return _card.api_base
        return _API_BASES.get(self.provider, _API_BASES["gitcode"])

    @property
    def clone_url(self) -> str:
        """返回仓库的 git clone URL（平台卡优先，无卡回退存量表）。"""
        _card = _card_for(self.provider)
        host = _card.host if (_card is not None and _card.host) else \
            _HOSTS.get(self.provider, _HOSTS["gitcode"])
        return f"https://{host}/{self.owner}/{self.name}.git"

    @property
    def bot_token(self) -> str:
        """委托 service 层获取 token（R20.11）。

        R20.11.1: 改为调 src/kernel/token_service.py 的 get_repo_bot_token()，
        内部委托 TokenResolver.get_token()——所有调 Repo.bot_token 的场景
        自动继承短路/过期检测。

        R20.11.5: 不直接 import TokenResolver，避免循环依赖。
        签名不变（str → str），向后兼容。
        """
        from src.kernel.token_service import get_repo_bot_token
        return get_repo_bot_token(self.repo_key)
