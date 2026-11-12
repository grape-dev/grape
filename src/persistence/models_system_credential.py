"""SystemCredentialModel——grape_t_system_credentials（D100 批 1）。

系统/组织级凭据存储面（方案稿 §1.3）：
- 爆炸半径=全平台，写入通道唯一=门面（src/domains/mgmt/user/grape_t_system_credentials.py），
  本模型不做独立写暴露（repositories 仅门面内部使用）
- scope_type 值域 {'system'}（org 预留不实现）；scope_id system 固定 ''
- created_at/updated_at NOT NULL 无 DEFAULT——时间由门面应用层强制赋值
  （铁律 2；DEFAULT '' 会让漏传静默成空串，时间审计失真）
- value 只存 credential_crypto 加密后密文，本表零明文

形态对齐 S79 ORM 三件套先例（UserCredentialModel 同构）。
"""
from __future__ import annotations

from sqlalchemy import Column, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped

from src.persistence.base import Base
from src.persistence.masking import masked_repr


class SystemCredentialModel(Base):
    """grape_t_system_credentials——系统/组织级凭据（cred_type → 加密 value）。"""
    __tablename__ = "grape_t_system_credentials"
    id: Mapped[str] = Column("id", String(36), primary_key=True)
    scope_type: Mapped[str] = Column("scope_type", String(16), nullable=False, default="system")
    scope_id: Mapped[str] = Column("scope_id", String(64), nullable=False, default="")
    cred_type: Mapped[str] = Column("cred_type", String(64), nullable=False)
    value: Mapped[str] = Column("value", Text, nullable=False)
    created_at: Mapped[str] = Column("created_at", Text, nullable=False, default="")
    updated_at: Mapped[str] = Column("updated_at", Text, nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "cred_type",
                         name="uq_sc_scope_cred"),
        {"extend_existing": True},
    )

    def __repr__(self):
        return masked_repr(self)
