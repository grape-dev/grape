"""GitCode 方言（存量委托：v5 form 语义）。"""

from src.kernel.config import log
from src.kernel.ports import accounts as _accounts
from src.kernel.ports import im as _im
from src.kernel.ports import repos as _repos
from src.kernel.ports import tokens as _tokens

from .base import GenericVcsProvider, VCSProvider, _require_card


class GitCodeProvider(GenericVcsProvider):
    """GitCode 平台（D22：GenericVcsProvider 基座 + 存量委托覆盖）。

    基座提供：verify_token（/user 卡鉴权）、存储字段（卡）、fetch_user_token
    （D3 统一入口 → user_credentials PAT）。
    覆盖仅保留协议验证过的存量委托：collaborator 邀请流与 webhook 创建
    （v5 form/事件集语义，repo_manager 存量序列）——批次 1 验证后退役。
    """

    def __init__(self):
        super().__init__(_require_card("gitcode"))

    def get_user_login(self, token: str) -> str:
        # D85 迁移修复：身份探测委托存量网络缝 repo_manager.get_user_gitcode_login
        # （与 GitHubProvider 对称；verify_token 直连导致 mock 缝失效/实网调用）
        return _repos.get_user_gitcode_login(token)

    def add_collaborator(self, api_token, owner, repo, username, permission="maintainer"):
        ok, _err = _repos.add_collaborator(api_token, owner, repo, username, permission)
        return ok

    def remove_collaborator(self, api_token, owner, repo, username):
        try:
            _repos.remove_collaborator(api_token, owner, repo, username)
            return True
        except Exception:
            return False

    def accept_collaborator_invite(self, bot_token, owner, repo):
        # D39/S59：存量邀请制语义（v5 邀请列表→匹配→接受）
        return _repos.accept_invite_for_repo(bot_token, owner, repo)

    def create_repo(self, api_token, name, description="", private=False,
                    owner="", path="", auto_init=False, default_branch="main") -> dict:
        # D42/S62：存量委托（v5 form 语义；owner=组织空间，path=仓库 URL 路径）
        return _repos.create_repo(api_token=api_token, name=name, description=description,
                                  private=private, owner=owner, path=path,
                                  auto_init=auto_init, default_branch=default_branch)

    def find_existing_webhook(self, api_token, owner, repo, webhook_url):
        # D42/S62：存量委托（含 429/5xx 退避）
        return _repos.find_existing_webhook(api_token, owner, repo, webhook_url)

    def fetch_repos_with_role(self, api_token) -> "list[dict]":
        # D42/S62：存量委托（成员仓库+is_admin 标注）
        return _repos.fetch_repos_with_role(api_token)

    def get_collaborator_permission(self, api_token, owner, repo, username):
        # D42/S62：存量委托（返回 permission dict，含 access_level）
        return _repos.get_member_permission(api_token, owner, repo, username)

    def list_collaborators(self, api_token, owner, repo):
        # D44/S66：存量委托（feishu_async 存量实现，含分页与字段形态）
        return _im._fetch_gitcode_collaborators(owner, repo, api_token)

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        return _repos.setup_webhook(api_token, owner, repo, webhook_url, webhook_password=webhook_secret)

    def setup_repo_webhook(self, bot_token, owner, repo, webhook_url):
        # D38/S58：存量形态 v5 password=bot_token（形态在适配器，调用方零分支）
        return _repos.setup_webhook(bot_token, owner, repo, webhook_url, webhook_password=bot_token)

    def remove_webhook(self, api_token, owner, repo, hook_id):
        try:
            _repos.remove_webhook(api_token, owner, repo, int(hook_id))
            return True
        except Exception:
            return False
