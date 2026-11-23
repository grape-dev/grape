"""GitHub 方言（卡驱动基座 + issues 形态差异）。"""

from src.kernel.config import log
from src.kernel.ports import accounts as _accounts
from src.kernel.ports import im as _im
from src.kernel.ports import repos as _repos
from src.kernel.ports import tokens as _tokens

from .base import GenericVcsProvider, VCSProvider, _require_card


class GitHubProvider(GenericVcsProvider):
    """GitHub 平台（D22：GenericVcsProvider 基座 + 存量委托覆盖，同 GitCode 说明）。"""

    def __init__(self):
        super().__init__(_require_card("github"))
        # resolve_repo_token/fetch_owner_identity_token 走 GenericVcsProvider 基类：
        # 卡能力位 owner_identity_token_fallback=true（configs/platforms/github.json）
        # 派生回落语义，基类默认实现按 provider_name="github" 查 external_identities
        # ——与原 db.get_github_token 尾段（D32-补丁）同链等价。

    def get_user_login(self, token: str) -> str:
        return _repos.get_user_github_login(token)

    def add_collaborator(self, api_token, owner, repo, username, permission="push"):
        return _repos.add_github_collaborator(api_token, owner, repo, username, permission)

    def remove_collaborator(self, api_token, owner, repo, username):
        try:
            _repos.remove_github_collaborator(api_token, owner, repo, username)
            return True
        except Exception:
            return False

    def accept_collaborator_invite(self, bot_token, owner, repo):
        # D39/S59：存量邀请制语义（PATCH /user/repository_invitations）
        return _repos.accept_github_invite(bot_token, owner, repo)

    def create_repo(self, api_token, name, description="", private=False,
                    owner="", path="", auto_init=False, default_branch="") -> dict:
        # D42/S62：存量委托（owner=组织空间；GitHub 仓库路径恒等于 name，path 忽略）
        return _repos.create_github_repo(api_token=api_token, name=name, description=description,
                                         private=private, owner=owner, path=path,
                                         auto_init=auto_init)

    def find_existing_webhook(self, api_token, owner, repo, webhook_url):
        # D42/S62：存量委托（FR-5.1 空列表=None 不误判）
        return _repos.find_existing_github_webhook(api_token, owner, repo, webhook_url)

    def fetch_repos_with_role(self, api_token) -> "list[dict]":
        # D42/S62：存量委托
        return _repos.fetch_github_repos_with_role(api_token)

    def get_collaborator_permission(self, api_token, owner, repo, username):
        # D42/S62：存量委托（返回 role_name 字符串）
        return _repos.get_github_collaborator_permission(api_token, owner, repo, username)

    def list_collaborators(self, api_token, owner, repo):
        # D44/S66：存量委托（feishu_async 存量实现，含分页与字段形态）
        return _im._fetch_github_collaborators(owner, repo, api_token)

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        return _repos.setup_github_webhook(api_token, owner, repo, webhook_url, webhook_secret)

    def setup_repo_webhook(self, bot_token, owner, repo, webhook_url):
        # D38/S58：存量形态 secret=""（bot token 不作 webhook 凭据，形态在适配器）
        return _repos.setup_github_webhook(bot_token, owner, repo, webhook_url, "")

    def remove_webhook(self, api_token, owner, repo, hook_id):
        try:
            _repos.remove_github_webhook(api_token, owner, repo, int(hook_id))
            return True
        except Exception:
            return False
