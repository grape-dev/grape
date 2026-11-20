"""D109 平台对象模型 ORM 映射（六表）。

纪律（d109 §4「领域建模与持久化」）：
- **只做映射，不 create_all**：建表一律走 `src/api/db_setup.py` 的 `register_migration`
  （版本 202611150007），避免双建表/双真源；
- 主键 UUID7（`src.kernel.id_gen.uuid7_str`，36 字符）；
- 凭据列（client_id_enc / client_secret_enc / token_enc）repr 全脱敏；
- D110 起 bot 归 `grape_t_repo_platforms` 行（bot_username / bot_token_enc / bot_token_status /
  bot_checked_at）；原 bot 账号目录表已删除，不再有部分唯一索引。
"""
from __future__ import annotations

from sqlalchemy import Column, Integer, Text, UniqueConstraint, VARCHAR, text

from src.kernel.persistence.base import Base
from src.kernel.persistence.masking import masked_repr


class OAuthChannelModel(Base):
    """grape_t_oauth_channels——登录渠道（配置派生 + 加密客户端凭据）。"""

    __tablename__ = "grape_t_oauth_channels"
    __table_args__ = {"extend_existing": True}

    id = Column(VARCHAR(36), primary_key=True)
    provider = Column("provider", Text, nullable=False, unique=True)
    name = Column("name", Text, default="")
    type = Column("type", Text, default="oauth2")
    auth_url = Column("auth_url", Text, default="")
    token_url = Column("token_url", Text, default="")
    user_url = Column("user_url", Text, default="")
    emails_endpoint = Column("emails_endpoint", Text, default="")
    scope = Column("scope", Text, default="")
    pkce_enabled = Column("pkce_enabled", Integer, default=0)
    id_field = Column("id_field", Text, default="id")
    login_field = Column("login_field", Text, default="login")
    name_field = Column("name_field", Text, default="name")
    avatar_field = Column("avatar_field", Text, default="")
    redirect_uri = Column("redirect_uri", Text, default="")
    brand_color = Column("brand_color", Text, default="")
    accept_header = Column("accept_header", Text, default="")
    client_id_enc = Column("client_id_enc", Text, default="")
    client_secret_enc = Column("client_secret_enc", Text, default="")
    repo_links_json = Column("repo_links_json", Text, default="[]")
    source = Column("source", Text, default="config")
    config_hash = Column("config_hash", Text, default="")
    enabled = Column("enabled", Integer, default=1)
    created_at = Column("created_at", Text, server_default=text("datetime(\'now\')"))
    updated_at = Column("updated_at", Text, server_default=text("datetime(\'now\')"))

    def __repr__(self):
        return masked_repr(self)


class RepoPlatformModel(Base):
    """grape_t_repo_platforms——仓库平台方言声明（不含凭据/账号）。"""

    __tablename__ = "grape_t_repo_platforms"
    __table_args__ = {"extend_existing": True}

    id = Column(VARCHAR(36), primary_key=True)
    provider = Column("provider", Text, nullable=False, unique=True)
    name = Column("name", Text, default="")
    dialect = Column("dialect", Text, default="")
    host = Column("host", Text, default="")
    api_base = Column("api_base", Text, default="")
    api_auth = Column("api_auth", Text, default="")
    webhook_path = Column("webhook_path", Text, default="")
    event_header = Column("event_header", Text, default="")
    webhook_verify = Column("webhook_verify", Text, default="")
    webhook_dialect = Column("webhook_dialect", Text, default="")
    pr_head_filter = Column("pr_head_filter", Text, default="")
    clone_auth = Column("clone_auth", Text, default="")
    issue_write_form = Column("issue_write_form", Text, default="")
    branch_create_form = Column("branch_create_form", Text, default="")
    issue_search = Column("issue_search", Integer, default=0)
    git_tree_family = Column("git_tree_family", Text, default="")
    pr_comment_endpoint = Column("pr_comment_endpoint", Text, default="")
    pr_labels_via_issues = Column("pr_labels_via_issues", Integer, default=0)
    workflows_write = Column("workflows_write", Integer, default=0)
    legacy_token_symbol = Column("legacy_token_symbol", Text, default="")
    # 原 grape_t_repo_credential_caps 的存活字段（2026-11-14 合表：drivers/oauth_requires_scope/
    # supports_bot_token/supports_webhook_secret 已删除语义，不再存储）
    pat_create_url = Column("pat_create_url", Text, default="")
    token_storage_field = Column("token_storage_field", Text, default="")
    webhook_token_field = Column("webhook_token_field", Text, default="")
    bot_username = Column("bot_username", Text, default="")   # D110：该平台唯一 bot 账号名（事实，管理面/卡播种）
    # D110：平台唯一 bot 的凭据与健康（一平台一个 bot；仓库级覆盖仍在 grape_t_repo_credentials('bot')）
    bot_token_enc = Column("bot_token_enc", Text, default="")          # 密文 PAT；空=该平台无全局 bot
    bot_token_status = Column("bot_token_status", Text, default="unknown")  # ok | expired | unknown（巡检写）
    bot_checked_at = Column("bot_checked_at", Text, default="")        # 最近一次探活时间
    owner_fallback = Column("owner_fallback", Integer, default=0)
    download_base = Column("download_base", Text, default="")
    user_images_base = Column("user_images_base", Text, default="")
    brand_color = Column("brand_color", Text, default="")
    source = Column("source", Text, default="config")
    config_hash = Column("config_hash", Text, default="")
    enabled = Column("enabled", Integer, default=1)
    created_at = Column("created_at", Text, server_default=text("datetime(\'now\')"))
    updated_at = Column("updated_at", Text, server_default=text("datetime(\'now\')"))

    def __repr__(self):
        return masked_repr(self)



class ChannelRepoLinkModel(Base):
    """grape_t_channel_repo_link——渠道→仓库平台的授权声明（受众位）。"""

    __tablename__ = "grape_t_channel_repo_link"
    __table_args__ = (
        UniqueConstraint("oauth_provider", "repo_provider", name="uq_channel_repo_link"),
        {"extend_existing": True},
    )

    id = Column(VARCHAR(36), primary_key=True)
    oauth_provider = Column("oauth_provider", Text, nullable=False)
    repo_provider = Column("repo_provider", Text, nullable=False)
    source = Column("source", Text, default="derived")
    config_hash = Column("config_hash", Text, default="")
    created_at = Column("created_at", Text, server_default=text("datetime(\'now\')"))
    updated_at = Column("updated_at", Text, server_default=text("datetime(\'now\')"))


class UserRepoPlatformLinkModel(Base):
    """grape_t_user_repo_platform_link——用户↔仓库平台的**纯对应关系**（2026-11-14）。

    只存关系与时间：不存 via_bits、不存 status、不存来源渠道（可用性由 credential_resolver 现算）。
    """

    __tablename__ = "grape_t_user_repo_platform_link"
    __table_args__ = (
        UniqueConstraint("user_id", "repo_provider", name="uq_user_repo_platform_link"),
        {"extend_existing": True},
    )

    id = Column(VARCHAR(36), primary_key=True)
    user_id = Column("user_id", Text, nullable=False)
    repo_provider = Column("repo_provider", Text, nullable=False)
    linked_at = Column("linked_at", Text, default="")
    revoked_at = Column("revoked_at", Text, default="")
    revoked_reason = Column("revoked_reason", Text, default="")
    created_at = Column("created_at", Text, server_default=text("datetime(\'now\')"))
    updated_at = Column("updated_at", Text, server_default=text("datetime(\'now\')"))
