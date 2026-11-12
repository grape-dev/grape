"""Repository 出入 DTO（D53 评审项：ORM 实体不出口）。

形态标准（业界 lotus-core Output-Shape 先例）：Repository 公开方法只返回
本模块 dataclass / primitive；ORM row 仅在仓储内部使用。守卫用例静态
扫描返回注解。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountDTO:
    id: str
    username: str
    email: str = ""
    nickname: str = ""
    status: int = 1
    token_version: int = 0


@dataclass(frozen=True)
class IdentityDTO:
    id: str
    grape_user_id: str
    provider: str
    external_id: str
    external_login: str = ""
    external_name: str = ""
    external_avatar: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token_expires_at: str = ""
    status: int = 1
    created_at: str = ""
    metadata_json: str = "{}"


@dataclass(frozen=True)
class UserCredentialDTO:
    user_id: str
    provider: str
    config_json: str = "{}"


@dataclass(frozen=True)
class ManagedRepoDTO:
    repo_key: str
    repo_owner: str
    repo_name: str
    full_name: str
    webhook_id: int = 0
    host_status: str = ""
    host_user_id: str = ""
    webhook_secret: str = ""
    org_id: str = ""
    bot_token_status: str = ""


@dataclass(frozen=True)
class RepoCredentialDTO:
    repo_key: str
    cred_type: str
    value: str


@dataclass(frozen=True)
class SystemCredentialDTO:
    """D100 批 1：系统/组织级凭据记录。

    value 字段为**解密后明文**——DTO 只由门面构造（repositories 层拿到的是
    ORM 密文，解密在门面完成），任何绕过门面的读取点都会拿到密文而非明文，
    这是刻意的失败形态（错用立刻可见，不会静默泄露也不会静默误用）。
    """
    id: str
    scope_type: str
    scope_id: str
    cred_type: str
    value: str = ""          # 解密后明文（仅门面构造路径）
    created_at: str = ""
    updated_at: str = ""
