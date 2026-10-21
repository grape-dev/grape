"""Token 获取统一接口（策略模式 + 职责链）。

Issue #372 R18: 统一 13 个散落函数为 TokenResolver 策略链。

设计文档: docs/weekly-08/token-config/04-token-resolver.md

模块结构:
  VCSProvider (ABC)
    ├── GitCodeProvider
    └── GitHubProvider
  TokenStrategy (基类)
    ├── DevopsAgentSessionToken  (第 1 级)
    ├── RepoBotToken             (第 2 级)
    ├── GlobalBotToken           (第 3 级)
    └── UserToken                (第 4 级)
  TokenResult (数据类)
  TokenResolver (统一入口)

三个语义入口:
  get_token()           — 获取可用执行 token（含过期短路）
  read_stored_token()   — 读取存储原始值（不过期检查、不刷新）
  resolve()             — 获取 TokenResult（token + level + provider）
  detect_and_mark_expired() — fail_task 401 统一检测 + 标记
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from src.config import log


# ---------------------------------------------------------------------------
# VCSProvider 接口
# ---------------------------------------------------------------------------

class VCSProvider(ABC):
    """VCS 平台接口。GitCode / GitHub 各自实现。

    策略类通过此接口调用平台能力，不硬编码 provider 分支。
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """provider 标识: "gitcode" / "github"。"""

    @abstractmethod
    def verify_token(self, token: str) -> dict:
        """验证 token 有效性，返回用户信息 dict（含 login 字段）。
        token 无效时抛 RuntimeError。"""

    @abstractmethod
    def get_user_login(self, token: str) -> str:
        """获取 token 对应的用户名。"""

    @abstractmethod
    def add_collaborator(self, api_token: str, owner: str, repo: str,
                         username: str, permission: str) -> bool:
        """邀 bot 为协作者。幂等：已是协作者返回 True。"""

    @abstractmethod
    def remove_collaborator(self, api_token: str, owner: str, repo: str,
                            username: str) -> bool:
        """移除协作者。best-effort，失败不阻断。"""

    @abstractmethod
    def setup_webhook(self, api_token: str, owner: str, repo: str,
                      webhook_url: str, webhook_secret: str) -> int:
        """创建 webhook，返回 webhook_id。"""

    @abstractmethod
    def remove_webhook(self, api_token: str, owner: str, repo: str,
                       hook_id: str) -> bool:
        """删除 webhook。"""

    @abstractmethod
    def token_storage_field(self) -> str:
        """仓库级 bot token 存储列名。"""

    @abstractmethod
    def webhook_token_field(self) -> str:
        """webhook token 存储列名。"""

    @abstractmethod
    def session_token_field(self) -> str:
        """devops_agent_session token 列名。"""

    @abstractmethod
    def global_bot_meta_key(self) -> str:
        """全局 bot token meta key。"""

    @abstractmethod
    def global_bot_status_meta_key(self) -> str:
        """全局 bot token 过期状态 meta key（per-provider, R21.2）。"""

    @abstractmethod
    def pat_create_url(self) -> str:
        """PAT 创建页链接。"""

    @abstractmethod
    def fetch_user_token(self, username: str, user_id: int) -> str:
        """获取用户 token（带刷新）。
        GitCode → get_user_token_with_refresh, GitHub → get_user_github_token_with_refresh。"""


class GitCodeProvider(VCSProvider):
    """GitCode 平台实现。"""

    API_BASE = "https://gitcode.com/api/v5"

    @property
    def provider_name(self) -> str:
        return "gitcode"

    def verify_token(self, token: str) -> dict:
        from src.user.repo_manager import _req
        return _req("GET", f"{self.API_BASE}/user", token)

    def get_user_login(self, token: str) -> str:
        data = self.verify_token(token)
        return data.get("login", "") or ""

    def add_collaborator(self, api_token, owner, repo, username, permission="maintainer"):
        from src.user.repo_manager import add_collaborator
        ok, _err = add_collaborator(api_token, owner, repo, username, permission)
        return ok

    def remove_collaborator(self, api_token, owner, repo, username):
        from src.user.repo_manager import remove_collaborator
        try:
            remove_collaborator(api_token, owner, repo, username)
            return True
        except Exception:
            return False

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        from src.user.repo_manager import setup_webhook
        return setup_webhook(api_token, owner, repo, webhook_url, webhook_password=webhook_secret)

    def remove_webhook(self, api_token, owner, repo, hook_id):
        from src.user.repo_manager import remove_webhook
        try:
            remove_webhook(api_token, owner, repo, int(hook_id))
            return True
        except Exception:
            return False

    def token_storage_field(self) -> str:
        return "bot_gitcode_token"

    def webhook_token_field(self) -> str:
        return "gitcode_webhook_token"

    def session_token_field(self) -> str:
        return "repo_gitcode_token"

    def global_bot_meta_key(self) -> str:
        return "global_bot_gitcode_token"

    def global_bot_status_meta_key(self) -> str:
        return "global_gitcode_bot_token_status"

    def pat_create_url(self) -> str:
        return "https://gitcode.com/profile/personal_access_tokens"

    def fetch_user_token(self, username: str, user_id: int) -> str:
        from src.api.deps import get_user_token_with_refresh
        return get_user_token_with_refresh(username, user_id) or ""


class GitHubProvider(VCSProvider):
    """GitHub 平台实现。"""

    API_BASE = "https://api.github.com"

    @property
    def provider_name(self) -> str:
        return "github"

    def verify_token(self, token: str) -> dict:
        from src.user.repo_manager import _github_req
        return _github_req("GET", f"{self.API_BASE}/user", token)

    def get_user_login(self, token: str) -> str:
        from src.user.repo_manager import get_user_github_login
        return get_user_github_login(token)

    def add_collaborator(self, api_token, owner, repo, username, permission="push"):
        from src.user.repo_manager import add_github_collaborator
        return add_github_collaborator(api_token, owner, repo, username, permission)

    def remove_collaborator(self, api_token, owner, repo, username):
        from src.user.repo_manager import remove_github_collaborator
        try:
            remove_github_collaborator(api_token, owner, repo, username)
            return True
        except Exception:
            return False

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        from src.user.repo_manager import setup_github_webhook
        return setup_github_webhook(api_token, owner, repo, webhook_url, webhook_secret=webhook_secret)

    def remove_webhook(self, api_token, owner, repo, hook_id):
        from src.user.repo_manager import remove_github_webhook
        try:
            remove_github_webhook(api_token, owner, repo, int(hook_id))
            return True
        except Exception:
            return False

    def token_storage_field(self) -> str:
        return "bot_github_token"

    def webhook_token_field(self) -> str:
        return "github_webhook_token"

    def session_token_field(self) -> str:
        return "repo_github_token"

    def global_bot_meta_key(self) -> str:
        return "global_bot_github_token"

    def global_bot_status_meta_key(self) -> str:
        return "global_github_bot_token_status"

    def pat_create_url(self) -> str:
        return "https://github.com/settings/tokens"

    def fetch_user_token(self, username: str, user_id: int) -> str:
        from src.api.deps import get_user_github_token_with_refresh
        return get_user_github_token_with_refresh(username, user_id) or ""


def create_provider(provider: str) -> VCSProvider:
    """根据 provider 标识创建 VCSProvider 实例。

    provider: "github" / "gitcode"（默认）
    """
    if provider == "github":
        return GitHubProvider()
    return GitCodeProvider()


def create_provider_from_repo_key(repo_key: str) -> VCSProvider:
    """根据 repo_key 前缀创建 VCSProvider。"""
    if repo_key and repo_key.startswith("github:"):
        return GitHubProvider()
    return GitCodeProvider()


# ---------------------------------------------------------------------------
# TokenResult 数据类
# ---------------------------------------------------------------------------

@dataclass
class TokenResult:
    """token 获取结果，携带级别信息。"""
    token: str = ""               # token 字符串（向后兼容：薄包装取 .token 返回 str）
    level: str = ""               # "session" / "repo_bot" / "global_bot" / "user"
    provider: Optional[VCSProvider] = None  # VCSProvider 实例
    repo_key: str = ""            # 仓库标识
    expired: bool = False         # 是否已标记过期（用户 token status=4 时 expired=True 但仍返回 token）
    issue_id: str = ""            # R20.6.1: 自循环检测用——is_self_loop_actor 需要查 running task


# ---------------------------------------------------------------------------
# TokenStrategy 策略基类
# ---------------------------------------------------------------------------

class TokenStrategy:
    """token 获取策略基类。每个子类代表 fallback 链中的一级。

    方法分层契约（关键——决定了上层 get_token / read_stored_token 的语义分离）：
    - _fetch(): 纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。
      供 read_stored_token() 和 get_token() 共用，是两者的公共底层。
    - _is_expired(): 查过期标记。只读标记字段，不调 VCS API。
    - get_token(): = _fetch() + _is_expired() 检查。过期则短路返回空串。
      这是"执行 token"语义——需要当前可用的 token。
    """

    error_code = ""  # 子类覆盖

    def __init__(self, provider: VCSProvider):
        self.provider = provider

    def get_token(self, repo_key: str) -> str:
        """获取可用执行 token。_fetch 读值 → _is_expired 检查过期 → 过期则短路返回空串。"""
        token = self._fetch(repo_key)
        if token and self._is_expired(repo_key):
            return ""  # 短路：已标记过期，不返回
        return token

    def _normalize_repo_key(self, repo_key: str) -> str:
        """标准化 repo_key（to_repo_key）。空则返回空串。子类复用避免重复 import。"""
        from src.repo_key import to_repo_key
        rk = to_repo_key(repo_key)
        return rk or ""

    def _is_path_b(self, repo_key: str) -> bool:
        """路径 B 仓库判断（host_user_id > 0）。子类复用避免重复 import get_host_user_id。"""
        return self._get_host_user_id(repo_key) > 0

    def _get_host_user_id(self, repo_key: str) -> int:
        """获取 host_user_id。子类复用避免重复 import。"""
        from src.user.db import get_host_user_id
        return get_host_user_id(repo_key)

    @abstractmethod
    def _fetch(self, repo_key: str) -> str:
        """纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。"""
        raise NotImplementedError

    def _is_expired(self, repo_key: str) -> bool:
        """查过期标记。只读标记字段，不调 VCS API 验证。默认 False。"""
        return False

    def mark_expired(self, repo_key: str):
        """子类实现：标记 token 过期。"""
        pass

    def clear_expired(self, repo_key: str):
        """子类实现：清除过期标记。"""
        pass

    @property
    def level(self) -> str:
        """策略级别标识。子类覆盖。"""
        return ""


# ---------------------------------------------------------------------------
# 四级策略实现
# ---------------------------------------------------------------------------

class DevopsAgentSessionToken(TokenStrategy):
    """第 1 级：devops_agent_session.{session_token_field}"""

    error_code = "SESSION_TOKEN_EXPIRED"

    @property
    def level(self) -> str:
        return "session"

    def _fetch(self, repo_key: str) -> str:
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return ""
        # R22.2: 路径 B 仓库（host_user_id > 0）跳过 session token
        # 与 RepoBotToken/GlobalBotToken 策略的路径 B 守卫对齐
        if self._is_path_b(repo_key):
            return ""
        field = self.provider.session_token_field()  # repo_gitcode_token / repo_github_token
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT {field} FROM devops_agent_session "
                    f"WHERE repo_key=? AND {field} IS NOT NULL "
                    f"AND {field}!='' LIMIT 1",
                    (repo_key,),
                )
                row = c.fetchone()
                if row and row[0]:
                    return row[0]
        except Exception as e:
            # R16.4.4: 列不存在时（未 ALTER）降级为返回空，不报错
            log.debug(f"DevopsAgentSessionToken._fetch: {e}")
        return ""

    def _is_expired(self, repo_key: str) -> bool:
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return False
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT token_status FROM devops_agent_session "
                    "WHERE repo_key=? LIMIT 1",
                    (repo_key,),
                )
                row = c.fetchone()
                if row and row[0] == "expired":
                    return True
        except Exception:
            # R16.4.4: token_status 列不存在时降级为不检查
            pass
        return False

    def mark_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE devops_agent_session SET token_status='expired' "
                    "WHERE repo_key=?",
                    (repo_key,),
                )
                db.commit()
        except Exception as e:
            log.warning(f"DevopsAgentSessionToken.mark_expired: {e}")

    def clear_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE devops_agent_session SET token_status='' "
                    "WHERE repo_key=?",
                    (repo_key,),
                )
                db.commit()
        except Exception as e:
            log.warning(f"DevopsAgentSessionToken.clear_expired: {e}")


class RepoBotToken(TokenStrategy):
    """第 2 级：managed_repos.{token_storage_field}"""

    error_code = "REPO_TOKEN_EXPIRED"

    @property
    def level(self) -> str:
        return "repo_bot"

    def _fetch(self, repo_key: str) -> str:
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return ""
        # 路径 B 仓库跳过仓库级 bot token（降级场景脏写入防护）
        if self._is_path_b(repo_key):
            return ""
        field = self.provider.token_storage_field()  # bot_gitcode_token / bot_github_token
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT {field} FROM managed_repos "
                    f"WHERE repo_key=? AND {field} IS NOT NULL "
                    f"AND {field}!='' LIMIT 1",
                    (repo_key,),
                )
                row = c.fetchone()
                if row and row[0]:
                    return row[0]
        except Exception as e:
            log.warning(f"RepoBotToken._fetch: {e}")
        return ""

    def _is_expired(self, repo_key: str) -> bool:
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return False
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT bot_token_status FROM managed_repos "
                    "WHERE repo_key=? LIMIT 1",
                    (repo_key,),
                )
                row = c.fetchone()
                if row and row[0] == "expired":
                    return True
        except Exception:
            pass
        return False

    def mark_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE managed_repos SET bot_token_status='expired' "
                    "WHERE repo_key=?",
                    (repo_key,),
                )
                db.commit()
        except Exception as e:
            log.warning(f"RepoBotToken.mark_expired: {e}")

    def clear_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE managed_repos SET bot_token_status='' "
                    "WHERE repo_key=?",
                    (repo_key,),
                )
                db.commit()
        except Exception as e:
            log.warning(f"RepoBotToken.clear_expired: {e}")


class GlobalBotToken(TokenStrategy):
    """第 3 级：全局 bot token（meta 表 {global_bot_meta_key}）"""

    error_code = "GLOBAL_TOKEN_EXPIRED"

    @property
    def level(self) -> str:
        return "global_bot"

    def _fetch(self, repo_key: str) -> str:
        # 路径 B 仓库跳过全局 bot token（§13.1）
        if self._is_path_b(repo_key):
            return ""
        # 复用现有 _cached_get_meta 缓存逻辑
        meta_key = self.provider.global_bot_meta_key()
        from src.user.db import _cached_get_meta, _read_gbt_file_field
        if self.provider.provider_name == "github":
            return _cached_get_meta(meta_key,
                                    file_fallback_fn=lambda: _read_gbt_file_field("github_bot_token"))
        return _cached_get_meta(meta_key,
                                file_fallback_fn=lambda: _read_gbt_file_field("gitcode_api_token"))

    def _is_expired(self, repo_key: str) -> bool:
        status_key = self.provider.global_bot_status_meta_key()
        from src.user.db import get_meta_value
        try:
            return get_meta_value(status_key) == "expired"
        except Exception:
            return False

    def mark_expired(self, repo_key: str):
        status_key = self.provider.global_bot_status_meta_key()
        from src.user.db import set_meta_value, _invalidate_global_bot_cache
        try:
            set_meta_value(status_key, "expired")
            _invalidate_global_bot_cache()
        except Exception as e:
            log.warning(f"GlobalBotToken.mark_expired: {e}")

    def clear_expired(self, repo_key: str):
        status_key = self.provider.global_bot_status_meta_key()
        from src.user.db import set_meta_value, _invalidate_global_bot_cache
        try:
            set_meta_value(status_key, "")
            _invalidate_global_bot_cache()
        except Exception as e:
            log.warning(f"GlobalBotToken.clear_expired: {e}")


class UserToken(TokenStrategy):
    """第 4 级：用户 token（external_identities，带刷新）。

    特殊行为：
    - _fetch 内部调 get_user_token_with_refresh（含 OAuth 自动刷新）
    - R25.3: _is_expired 检查 status=4 → 执行链短路返回空串（与其他层一致）
    - 回帖链通过 UserTokenHandler.get_comment_token() 调 _fetch() 绕过过期检查（best-effort）
    - 新增 _fetch_stored() 方法：只读 external_identities.access_token，不触发 refresh。
      供 read_stored_token() 调用。
    """

    error_code = "TOKEN_EXPIRED"

    @property
    def level(self) -> str:
        return "user"

    def _fetch(self, repo_key: str) -> str:
        from src.auth.grape_account_db import get_grape_account_by_id
        host_user_id = self._get_host_user_id(repo_key)
        if host_user_id == 0:
            return ""  # 路径 A 不走用户 token
        try:
            account = get_grape_account_by_id(host_user_id)
            if account and account.get("username"):
                return self.provider.fetch_user_token(account["username"], host_user_id)
        except Exception as e:
            log.warning(f"UserToken._fetch: {e}")
        return ""

    def _fetch_stored(self, repo_key: str) -> str:
        """只读 external_identities.access_token（解密），不触发 OAuth refresh。

        供 read_stored_token() 调用——A2A 鉴权 / session 写入只需原始值。
        """
        from src.auth.grape_account_db import get_grape_account_by_id
        from src.session_manager import db, db_lock
        from src.auth.credential_crypto import decrypt
        host_user_id = self._get_host_user_id(repo_key)
        if host_user_id == 0:
            return ""
        try:
            account = get_grape_account_by_id(host_user_id)
            if not account:
                return ""
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT access_token FROM external_identities "
                    "WHERE grape_user_id=? AND provider=? AND status IN (1, 4) LIMIT 1",
                    (account["id"], self.provider.provider_name),
                )
                row = c.fetchone()
            if row and row[0]:
                return decrypt(row[0])
        except Exception as e:
            log.warning(f"UserToken._fetch_stored: {e}")
        return ""

    def _is_expired(self, repo_key: str) -> bool:
        """R25.3: 检查 status=4（token 过期），执行链短路返回空串。

        与 DevopsAgentSessionToken / RepoBotToken / GlobalBotToken 三层一致——
        过期标记后 get_token() 短路，不再返回过期 token。

        回帖链通过 UserTokenHandler.get_comment_token() 调 _fetch() 绕过此检查，
        保留 best-effort 发评论能力。
        """
        host_user_id = self._get_host_user_id(repo_key)
        if host_user_id == 0:
            return False  # 路径 A 仓库不走用户 token
        from src.auth.grape_account_db import is_identity_expired
        return is_identity_expired(host_user_id, self.provider.provider_name)

    def mark_expired(self, repo_key: str):
        host_user_id = self._get_host_user_id(repo_key)
        if host_user_id > 0:
            from src.auth.grape_account_db import mark_identity_expired
            mark_identity_expired(host_user_id, self.provider.provider_name)

    def clear_expired(self, repo_key: str):
        """用户重新绑定时 create/update_external_identity 已恢复 status=1，无需额外操作。"""
        pass


# ---------------------------------------------------------------------------
# TokenResolver 统一入口
# ---------------------------------------------------------------------------

class TokenResolver:
    """token 获取统一入口。按优先级链执行策略，第一个返回非空 token 的策略胜出。

    三个语义入口：
    - get_token(): 获取可用执行 token（含过期短路）。供 pipeline / agent / clone / push 用。
    - read_stored_token(): 读取存储原始值（不过期检查、不刷新）。供 A2A 鉴权 / session 写入用。
    - resolve(): 获取 TokenResult（token + level + provider）。供需要级别差异的调用方用。
    - resolve_config_level(): 获取配置级别（不检查过期、不触发 refresh）。供自循环检测用。
    - detect_and_mark_expired(): fail_task 401 统一检测 + 标记。
    """

    def __init__(self, provider: VCSProvider):
        self.provider = provider
        self.strategies = [
            DevopsAgentSessionToken(provider),
            RepoBotToken(provider),
            GlobalBotToken(provider),
            UserToken(provider),
        ]

    def get_token(self, repo_key: str) -> str:
        """获取可用执行 token。含过期短路。返回空串=无可用 token。"""
        if not repo_key:
            return ""
        from src.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return ""
        for strategy in self.strategies:
            token = strategy.get_token(repo_key)
            if token:
                return token
        return ""

    def read_stored_token(self, repo_key: str) -> str:
        """读取存储的原始 token 值。不过期检查、不触发 OAuth refresh、不调 VCS API。

        契约（写死，不可变）：
        1. 不检查过期标记——即使 token 被标记 expired 也返回原始值
        2. 不触发 OAuth refresh——UserToken 策略用 _fetch_stored 跳过 refresh
        3. 走缓存层——_cached_get_meta 的 60s 缓存仍生效
        4. 返回空串=该 repo 无任何 token 存储（不是"过期"，是"未配置"）

        供 A2A 鉴权（get_repo_bot_token_no_global）/ session 写入用。
        """
        if not repo_key:
            return ""
        from src.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return ""
        for strategy in self.strategies:
            # UserToken 策略用 _fetch_stored 跳过 refresh
            if hasattr(strategy, '_fetch_stored'):
                token = strategy._fetch_stored(repo_key)
            else:
                token = strategy._fetch(repo_key)
            if token:
                return token
        return ""

    def resolve(self, repo_key: str) -> TokenResult:
        """获取 TokenResult（token + level + provider + repo_key + expired）。

        遍历策略链，第一个返回非空 token 的策略胜出。
        返回的 TokenResult 携带级别信息供 TokenActionHandler 分发。
        """
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        from src.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        for strategy in self.strategies:
            token = strategy.get_token(repo_key)
            if token:
                expired = strategy._is_expired(repo_key)
                return TokenResult(
                    token=token,
                    level=strategy.level,
                    provider=self.provider,
                    repo_key=repo_key,
                    expired=expired,
                )
        return TokenResult(token="", level="", provider=self.provider, repo_key=repo_key)

    def resolve_config_level(self, repo_key: str) -> TokenResult:
        """获取配置级别 TokenResult（不检查过期、不触发 refresh）。

        供自循环检测（handler.is_self_loop_actor）用——判断仓库的 token 配置级别，
        不需要实际 token 值（可能已过期），只需要知道是路径 A（bot token）还是路径 B（user token）。

        与 resolve() 的区别：
        - resolve() 调 get_token()（含过期短路），token 过期时可能跳到下一级 → level 不准确
        - resolve_config_level() 调 _fetch()（纯读，不检查过期），返回真实配置级别

        UserToken 策略用 _fetch_stored 跳过 OAuth refresh。
        """
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        from src.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        for strategy in self.strategies:
            if hasattr(strategy, '_fetch_stored'):
                token = strategy._fetch_stored(repo_key)
            else:
                token = strategy._fetch(repo_key)
            if token:
                return TokenResult(
                    token=token,
                    level=strategy.level,
                    provider=self.provider,
                    repo_key=repo_key,
                )
        # 无 token 配置 → 返回空级别（pipeline 不检测自循环）
        return TokenResult(token="", level="", provider=self.provider, repo_key=repo_key)

    def detect_and_mark_expired(self, repo_key: str) -> str:
        """fail_task 401 时调用：按优先级找到当前生效的策略，验证 token 有效性后标记过期。

        流程：
        1. 遍历策略链，用 _fetch（纯读，不检查过期标记）找到第一个有 token 的策略
        2. 调 provider.verify_token(token) 验证 token 有效性
           - 验证有效 → 401 是其他原因（权限不足等），不标记过期，返回 ""
           - 验证 401 → 标记过期（幂等：已标记则不重复），返回 error_code
           - 验证超时/5xx → 不标记（R21.3.5），返回 ""（401 可能是其他原因）
        3. 所有策略 _fetch 都返回空 → 返回 "NO_TOKEN_AVAILABLE"（R16.6.1）

        R21.3.5: 区分 401 和超时/5xx——只对 401 标记过期。
        R21.3.3: 原 R18.5.10 "超时视为验证失败" 设计取消。
        """
        from src.vcs.token_error import extract_http_status_from_exc
        for strategy in self.strategies:
            token = strategy._fetch(repo_key)  # 不检查过期标记，查原始值
            if token:
                # 幂等：已标记过则不重复标记（R16.1.6）
                if strategy._is_expired(repo_key):
                    return strategy.error_code  # 已标记，直接返回 error_code
                # 先验证 token 有效性（通过 provider 接口调 VCS API GET /user）
                try:
                    self.provider.verify_token(token)
                    # token 验证有效 → 401 是其他原因，不标记过期
                    return ""
                except Exception as e:
                    # R21.3.5: 区分 401 和超时/5xx
                    http_code = extract_http_status_from_exc(e)
                    if http_code == 401:
                        # token 验证 401 → 标记过期
                        strategy.mark_expired(repo_key)
                        return strategy.error_code
                    else:
                        # 超时/5xx → 不标记（R21.3.5），返回空
                        log.warning(f"TokenResolver.detect_and_mark_expired: verify non-401 error for {repo_key}: {str(e)[:100]}")
                        return ""
        # 所有策略 _fetch 都返回空（token 已被短路或未配置）→ 无可用 token
        return "NO_TOKEN_AVAILABLE"  # R16.6.1


# ---------------------------------------------------------------------------
# R18.6 TokenActionHandler 职责链
# ---------------------------------------------------------------------------

class TokenActionHandler(ABC):
    """R18.6.2: token 操作职责链接口。按级别差异化执行 token 相关操作。

    策略链负责"取 token + 级别标识"，职责链负责"按级别执行差异化操作"。
    """

    @abstractmethod
    def is_self_loop_actor(self, actor: str, event_type: str,
                           result: TokenResult) -> bool:
        """R18.6.8: 判断 actor 是否是 agent 回声（自循环检测）。"""

    @abstractmethod
    def get_comment_token(self, result: TokenResult) -> str:
        """R18.6.9: 获取用于发 Issue/PR 评论的 token。
        可能不同于执行 token——例如仓库级 bot 过期时 fallback 到全局 bot。"""

    @abstractmethod
    def post_issue_comment(self, repo_key: str, issue_num: str,
                           body: str, result: TokenResult) -> bool:
        """R18.6.10: 发 Issue 评论。按级别差异化处理 actor 和标记。
        成功返回 True，失败返回 False（调用方据此 append RESULT_POST_FAILED）。"""

    @abstractmethod
    def mark_expired(self, result: TokenResult) -> str:
        """标记当前级别 token 过期，返回 error_code。"""

    @abstractmethod
    def clear_expired(self, result: TokenResult):
        """清除当前级别过期标记（token 重新配置时调用）。"""


class SessionTokenHandler(TokenActionHandler):
    """R18.6.3: 第 1 级 session token。agent 专属配置，scope 可能受限。"""

    def is_self_loop_actor(self, actor, event_type, result):
        return False  # session token 的 actor 取决于 token 所有者，降级为不检测

    def get_comment_token(self, result):
        return ""  # session token scope 可能受限，不用于发评论

    def post_issue_comment(self, repo_key, issue_num, body, result):
        # session token 通常不用于发评论
        return False

    def mark_expired(self, result):
        # 写 devops_agent_session.token_status = 'expired'
        if result.provider and result.repo_key:
            DevopsAgentSessionToken(result.provider).mark_expired(result.repo_key)
        return "SESSION_TOKEN_EXPIRED"

    def clear_expired(self, result):
        if result.provider and result.repo_key:
            DevopsAgentSessionToken(result.provider).clear_expired(result.repo_key)


class RepoBotTokenHandler(TokenActionHandler):
    """R18.6.4: 第 2 级仓库级 bot token。单仓库独立 bot 账号。"""

    def is_self_loop_actor(self, actor, event_type, result):
        # actor = managed_repos.bot_username
        if not actor or not result.repo_key:
            return False
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("SELECT bot_username FROM managed_repos WHERE repo_key=? LIMIT 1", (result.repo_key,))
                row = c.fetchone()
                if row and row[0]:
                    return actor == row[0]
        except Exception:
            pass
        return False

    def get_comment_token(self, result):
        # 仓库级 bot 有效 → 用仓库级 bot token
        if result.token and not result.expired:
            return result.token
        # 仓库级 bot 过期 → fallback 到全局 bot
        if result.provider:
            try:
                from src.user.db import _get_global_bot_token, get_global_github_bot_token
                if result.provider.provider_name == "github":
                    return get_global_github_bot_token() or ""
                return _get_global_bot_token() or ""
            except Exception:
                pass
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result):
        comment_token = self.get_comment_token(result)
        if not comment_token or not result.provider:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        return _post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token)

    def mark_expired(self, result):
        if result.provider and result.repo_key:
            RepoBotToken(result.provider).mark_expired(result.repo_key)
        return "REPO_TOKEN_EXPIRED"

    def clear_expired(self, result):
        if result.provider and result.repo_key:
            RepoBotToken(result.provider).clear_expired(result.repo_key)


class GlobalBotTokenHandler(TokenActionHandler):
    """R18.6.5: 第 3 级全局 bot token。所有路径 A 仓库共用。"""

    def is_self_loop_actor(self, actor, event_type, result):
        if not actor:
            return False
        try:
            if result.provider.provider_name == "github":
                from src.user.db import get_global_github_bot_username
                _bot_name = get_global_github_bot_username()
            else:
                from src.user.db import get_global_bot_usernames
                _bot_names = get_global_bot_usernames()
                return actor in _bot_names if _bot_names else False
            return actor == _bot_name if _bot_name else False
        except Exception:
            return False

    def get_comment_token(self, result):
        # 全局 bot 有效 → 用全局 bot token
        if result.token and not result.expired:
            return result.token
        # 全局 bot 过期 → fallback 到仓库级 bot
        if result.provider and result.repo_key:
            try:
                from src.token_resolver import RepoBotToken
                _repo_strategy = RepoBotToken(result.provider)
                _repo_token = _repo_strategy._fetch(result.repo_key)
                if _repo_token and not _repo_strategy._is_expired(result.repo_key):
                    return _repo_token
            except Exception:
                pass
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result):
        comment_token = self.get_comment_token(result)
        if not comment_token or not result.provider:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        return _post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token)

    def mark_expired(self, result):
        if result.provider and result.repo_key:
            GlobalBotToken(result.provider).mark_expired(result.repo_key)
        return "GLOBAL_TOKEN_EXPIRED"

    def clear_expired(self, result):
        if result.provider and result.repo_key:
            GlobalBotToken(result.provider).clear_expired(result.repo_key)


class UserTokenHandler(TokenActionHandler):
    """R18.6.6: 第 4 级用户 token。路径 B 专属。

    核心矛盾：路径 B 下 agent 和用户用同一个 token，actor 完全相同。
    需要引入 task 执行状态作为额外维度（四条件组合判断）。
    """

    def is_self_loop_actor(self, actor, event_type, result):
        # R20.1.1: 四条件组合判断
        if event_type not in ("issue_comment", "note_hook_pr", "issue_updated"):
            return False
        if not result.repo_key:
            return False
        # 条件 1: 路径 B 仓库（通过 result.level == "user" 已确认）
        # 条件 2: actor == host_vcs_username
        try:
            from src.user.db import get_host_vcs_username
            host_vcs_username = get_host_vcs_username(result.repo_key, result.provider.provider_name if result.provider else "")
            if not host_vcs_username or actor != host_vcs_username:
                return False
        except Exception:
            return False
        # 条件 4: 该 issue 有正在执行的 task
        from src.api.event_pipeline import check_running_task
        return check_running_task(result.repo_key, result.issue_id or "")

    def get_comment_token(self, result):
        # R25.3: 回帖链 best-effort——直接用 _fetch 读存储 token（绕过 _is_expired 检查）
        # 执行链（get_token/resolve）对 status=4 短路返回空串，但回帖需要试发"Token 已过期"评论
        if result.provider and result.repo_key:
            try:
                _ut = UserToken(result.provider)
                _tok = _ut._fetch(result.repo_key)  # 绕过 _is_expired，best-effort
                if _tok:
                    return _tok
            except Exception:
                pass
        # 用户 token 完全不可用 → fallback 全局 bot token
        if result.provider:
            try:
                from src.user.db import _get_global_bot_token, get_global_github_bot_token
                if result.provider.provider_name == "github":
                    return get_global_github_bot_token() or ""
                return _get_global_bot_token() or ""
            except Exception:
                pass
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result):
        comment_token = self.get_comment_token(result)
        if not comment_token or not result.provider:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        try:
            return _post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token)
        except Exception:
            # 用户 token 过期 → 回帖失败 → R20.2 降级处理
            return False

    def mark_expired(self, result):
        if result.provider and result.repo_key:
            UserToken(result.provider).mark_expired(result.repo_key)
        return "TOKEN_EXPIRED"

    def clear_expired(self, result):
        # 用户重新绑定时 create/update_external_identity 已恢复 status=1
        pass


def create_action_handler(result: TokenResult) -> TokenActionHandler:
    """R18.6.7: 根据 TokenResult.level 创建对应 Handler。"""
    if result.level == "session":
        return SessionTokenHandler()
    elif result.level == "repo_bot":
        return RepoBotTokenHandler()
    elif result.level == "global_bot":
        return GlobalBotTokenHandler()
    elif result.level == "user":
        return UserTokenHandler()
    return None


def _post_comment_via_provider(provider: VCSProvider, repo_key: str,
                                issue_num: str, body: str, token: str) -> bool:
    """R18.6.10 辅助函数：通过 provider 接口发 Issue 评论。

    内部委托 event_pipeline._post_result_to_issue 的评论发送逻辑，
    但只发评论（不含 conclusion 提取等业务逻辑），加 grape-auto-comment 标记。
    """
    if not token or not repo_key or not issue_num:
        return False
    try:
        from src.api.event_pipeline import _post_result_to_issue, _is_github_repo
        from src.repo_key import from_repo_key
        _repo = from_repo_key(repo_key)
        if not _repo:
            return False
        # 复用现有 _post_result_to_issue 发评论（含去重、conclusion 提取等逻辑）
        _post_result_to_issue(_repo, str(issue_num), body, token, "agent", repo_key)
        return True
    except Exception as e:
        from src.config import log
        log.warning(f"_post_comment_via_provider failed: {e}")
        return False
