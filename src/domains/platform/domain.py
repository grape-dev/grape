"""D109 平台对象模型——领域层。

纪律（见 docs/weekly-09/multi-platform-decouple/d109-platform-object-model-design.md §3/§4）：
- 状态不落表：关系表只存对应关系，可用性/凭据由 `credential_resolver` 现算；
- `via_bits` 是位图：增减途径用纯位运算（`|` / `& ~`），不存在「读-改-写」丢失更新；
- 凭据值不进领域对象 repr；token 只在 Repository 解密后按需取用；
- 主键 UUID7（`src.kernel.id_gen.uuid7_str`），时间有序、索引友好。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntFlag


class ChannelType(str, Enum):
    OAUTH2 = "oauth2"
    OIDC = "oidc"
    SAML = "saml"


class TokenStatus(str, Enum):
    OK = "ok"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass
class OAuthChannel:
    """登录渠道聚合根（配置派生 + 加密凭据列）。"""

    provider: str
    name: str = ""
    type: str = ChannelType.OAUTH2.value
    auth_url: str = ""
    token_url: str = ""
    user_url: str = ""
    emails_endpoint: str = ""
    scope: str = ""
    pkce_enabled: bool = False
    id_field: str = "id"
    login_field: str = "login"
    name_field: str = "name"
    avatar_field: str = ""
    redirect_uri: str = ""
    brand_color: str = ""
    accept_header: str = ""
    repo_links: list[dict] = field(default_factory=list)
    enabled: bool = True

    def is_pkce_enabled(self) -> bool:
        return bool(self.pkce_enabled)


@dataclass
class RepoPlatform:
    """仓库平台聚合根（方言声明，不含任何凭据/账号）。"""

    provider: str
    name: str = ""
    dialect: str = ""
    host: str = ""
    api_base: str = ""
    api_auth: str = ""
    webhook_path: str = ""
    event_header: str = ""
    webhook_verify: str = ""
    webhook_dialect: str = ""
    pr_head_filter: str = ""
    clone_auth: str = ""
    create_repo_options: bool = False
    issue_search: bool = False
    enabled: bool = True

    def supports_webhook_dialect(self, dialect: str) -> bool:
        return bool(self.webhook_dialect) and self.webhook_dialect == dialect


@dataclass
class ChannelRepoLink:
    """渠道→仓库平台的授权声明（受众位）。"""

    oauth_provider: str
    repo_provider: str


@dataclass
class UserRepoPlatformLink:
    """用户↔仓库平台：**纯对应关系**（2026-11-14 裁定）。

    只回答"这个用户与这个平台是否存在接入关系"；可用性（用哪种凭据）由
    `credential_resolver` 在驱动时现算，不落表、不进本对象。
    """

    user_id: str
    repo_provider: str
    linked_at: str = ""
    revoked_at: str = ""
    revoked_reason: str = ""

    def link(self, now: str = "") -> None:
        """建立/恢复关系（幂等；任何来源重新获得即复活）。"""
        if not self.linked_at:
            self.linked_at = now
        self.revoked_at = ""
        self.revoked_reason = ""

    def revoke(self, now: str = "", reason: str = "") -> None:
        """撤销关系（写时间与原因；凭据不在此处删）。"""
        self.revoked_at = now
        self.revoked_reason = reason

    @property
    def active(self) -> bool:
        return not self.revoked_at
