"""四域 ORM 模型（S78）：逐列对齐存量 schema（PRAGMA 实测，2026-11-05）。

纪律：列变更 = 盖章迁移先行 + 本文件同步；漂移由 schema 对账守卫抓取。
敏感列（token/secret/password/config_json）repr 全脱敏。
"""
from __future__ import annotations

from sqlalchemy import Column, Integer, Text, String, VARCHAR, UniqueConstraint
from sqlalchemy.orm import Mapped

from src.kernel.persistence.base import Base
from src.kernel.persistence.masking import masked_repr


class AccountModel(Base):
    """grape_accounts——身份聚合根。"""
    __tablename__ = "grape_accounts"
    __table_args__ = {"extend_existing": True}  # 合并期容错：同名表被重复声明时覆盖而非抛错

    id: Mapped[str] = Column(String(36), primary_key=True)  # D105：UUID（存量行字符串化整型值）
    username: Mapped[str] = Column("username", String(64), nullable=False, unique=True)
    email: Mapped[str] = Column("email", String(255), nullable=True)
    email_source: Mapped[str] = Column("email_source", String(16), nullable=True)
    password_hash: Mapped[str] = Column("password_hash", Text, nullable=False, default="")
    nickname: Mapped[str] = Column("nickname", String(128), default="")
    avatar_url: Mapped[str] = Column("avatar_url", String(512), default="")
    status: Mapped[int] = Column("status", Integer, default=1)
    token_version: Mapped[int] = Column("token_version", Integer, default=0)
    email_verified: Mapped[int] = Column("email_verified", Integer, default=0)
    email_is_placeholder: Mapped[int] = Column("email_is_placeholder", Integer, default=0)
    created_by_oauth: Mapped[int] = Column("created_by_oauth", Integer, default=0)
    last_active_at: Mapped[str] = Column("last_active_at", Text, nullable=True)
    last_login_at: Mapped[str] = Column("last_login_at", Text, nullable=True)
    created_at: Mapped[str] = Column("created_at", Text, default="")
    updated_at: Mapped[str] = Column("updated_at", Text, default="")

    def __repr__(self):
        return masked_repr(self)


class ExternalIdentityModel(Base):
    """external_identities——第三方平台身份绑定（登录凭据域，D32）。"""
    __tablename__ = "external_identities"
    __table_args__ = {"extend_existing": True}  # 合并期容错：同名表被重复声明时覆盖而非抛错

    id: Mapped[str] = Column(String(36), primary_key=True)  # D105：UUID（存量值字符串化保留）
    grape_user_id: Mapped[str] = Column("grape_user_id", String(36), nullable=False)  # D105 第二批：引用账号主键（UUID/字符串化整型）
    provider: Mapped[str] = Column("provider", String(32), nullable=False)
    external_id: Mapped[str] = Column("external_id", String(128), nullable=False)
    external_login: Mapped[str] = Column("external_login", String(128), default="")
    external_name: Mapped[str] = Column("external_name", String(128), default="")
    external_avatar: Mapped[str] = Column("external_avatar", String(512), default="")
    access_token: Mapped[str] = Column("access_token", Text, default="")
    refresh_token: Mapped[str] = Column("refresh_token", Text, default="")
    token_expires_at: Mapped[str] = Column("token_expires_at", Text, default="")
    metadata_json: Mapped[str] = Column("metadata", Text, default="{}")
    status: Mapped[int] = Column("status", Integer, default=1)
    unbound_at: Mapped[str] = Column("unbound_at", Text, nullable=True)
    created_at: Mapped[str] = Column("created_at", Text, default="")
    updated_at: Mapped[str] = Column("updated_at", Text, default="")

    def __repr__(self):
        return masked_repr(self)


class UserCredentialModel(Base):
    """user_credentials——第三方凭据 KV（provider → config_json）。"""
    __tablename__ = "user_credentials"
    __table_args__ = {"extend_existing": True}  # 合并期容错：同名表被重复声明时覆盖而非抛错

    id: Mapped[str] = Column("id", String(36), primary_key=True)  # D105：UUID（N1 铁律 1，应用层生成）
    user_id: Mapped[str] = Column("user_id", String(36), primary_key=True)  # D105 第二批
    provider: Mapped[str] = Column("provider", Text, primary_key=True)
    # A2（2026-11-13）：凭据类型列（provider 存纯名，类型独立）。必须与迁移
    # 202611170006 的物理列一致；缺映射时走 ORM 插入会不带该列，MySQL 严格模式下
    # 报 1364 "Field 'kind' doesn't have a default value"（_strip_text_default 会把
    # TEXT NOT NULL DEFAULT '' 的默认值剥掉）。
    kind: Mapped[str] = Column("kind", Text, nullable=False, default="")
    config_json: Mapped[str] = Column("config_json", Text, nullable=False, default="{}")
    created_at: Mapped[str] = Column("created_at", Text, default="")
    updated_at: Mapped[str] = Column("updated_at", Text, default="")

    def __repr__(self):
        return masked_repr(self)


class ManagedRepoModel(Base):
    """managed_repos——仓库托管聚合（25 列，含 S64 平台派生列）。"""
    __tablename__ = "managed_repos"
    __table_args__ = {"extend_existing": True}  # 合并期容错：同名表被重复声明时覆盖而非抛错

    repo_key: Mapped[str] = Column("repo_key", Text, primary_key=True)
    repo_owner: Mapped[str] = Column("repo_owner", Text, nullable=False)
    repo_name: Mapped[str] = Column("repo_name", Text, nullable=False)
    full_name: Mapped[str] = Column("full_name", Text, nullable=False)
    webhook_id: Mapped[int] = Column("webhook_id", Integer, default=0)
    dev_scene: Mapped[str] = Column("dev_scene", Text, default="")
    host_status: Mapped[str] = Column("host_status", Text, default="")
    bot_gitcode_token: Mapped[str] = Column("bot_gitcode_token", Text, default="")
    gitcode_webhook_token: Mapped[str] = Column("gitcode_webhook_token", Text, default="")
    bot_username: Mapped[str] = Column("bot_username", Text, default="")
    host_user_id: Mapped[str] = Column("host_user_id", String(36), default="")  # D105 第二批
    webhook_secret: Mapped[str] = Column("webhook_secret", Text, default="")
    created_at: Mapped[str] = Column("created_at", Text, default="")
    bot_github_token: Mapped[str] = Column("bot_github_token", Text, default="")
    github_webhook_token: Mapped[str] = Column("github_webhook_token", Text, default="")
    scene_recommend_config: Mapped[str] = Column("scene_recommend_config", Text, default="")
    is_admin: Mapped[int] = Column("is_admin", Integer, default=0)
    bot_token_status: Mapped[str] = Column("bot_token_status", Text, default="")
    host_vcs_username: Mapped[str] = Column("host_vcs_username", Text, default="")
    deploy_url: Mapped[str] = Column("deploy_url", String(512), default="")
    org_id: Mapped[str] = Column("org_id", Text, default="")

    # F4/D106（凭据存储终态化第一批）：gitee/gitlab 四列撤出 ORM 模型——
    # 卡置空 token_storage_field/webhook_token_field 后冷启动不再建列，
    # gitee/gitlab 仓凭据唯一落点=grape_t_repo_credentials（存量库历史列不 DROP，
    # 由 D106 回填迁移+写路径置空渐进收敛）。

    def __repr__(self):
        return masked_repr(self)


class RepoCredentialModel(Base):
    """grape_t_repo_credentials——仓库凭据单表（S80/D55 建；D99 语义重设计：
    id 主键化（查询/引用以 id 为准）+ cred_type 归一值域 {'bot','webhook'}
    （凭据种类，平台语义由 repo_key 承载）。"""
    __tablename__ = "grape_t_repo_credentials"
    id: Mapped[str] = Column("id", VARCHAR(256), primary_key=True)
    repo_key: Mapped[str] = Column("repo_key", Text, nullable=False)
    cred_type: Mapped[str] = Column("cred_type", Text, nullable=False)
    # nullable=False 对齐 DDL（db_setup：value/updated_at 均 TEXT NOT NULL DEFAULT）——
    # s78 schema-drift 守卫按可空性逐列对账，遗留 Column 默认 nullable=True 判漂移红。
    value: Mapped[str] = Column("value", Text, nullable=False, default="")
    updated_at: Mapped[str] = Column("updated_at", Text, nullable=False, default="")
    __table_args__ = (
        UniqueConstraint("repo_key", "cred_type", name="uq_rc_repo_credtype"),
        {"extend_existing": True},   # 合并期容错：同名表被重复声明时覆盖而非抛错
    )

    def __repr__(self):
        return masked_repr(self)
