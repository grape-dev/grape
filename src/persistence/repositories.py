"""四域 Repository（D53：DTO 出入、接受外部 session、只 flush 不 commit）。

S91 收口：GRAPE_PERSISTENCE_ORM 开关已退役，调用方一律走本层（原 raw SQL
路径）——灰度/快速切回的回滚语义（D53 评审采纳）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.id_gen import uuid7_str as _uuid7
from src.persistence.dto import (AccountDTO, IdentityDTO, ManagedRepoDTO,
                                 RepoCredentialDTO, SystemCredentialDTO,
                                 UserCredentialDTO)
from src.persistence.models import (AccountModel, ExternalIdentityModel,
                                    ManagedRepoModel, RepoCredentialModel,
                                    UserCredentialModel)
from src.persistence.models_system_credential import SystemCredentialModel


def _account_dto(m: AccountModel) -> AccountDTO:
    return AccountDTO(id=m.id, username=m.username, email=m.email or "",
                      nickname=m.nickname or "", status=m.status,
                      token_version=m.token_version)


def _identity_dto(m: ExternalIdentityModel) -> IdentityDTO:
    return IdentityDTO(id=m.id, grape_user_id=m.grape_user_id, provider=m.provider,
                       external_id=m.external_id, external_login=m.external_login or "",
                       external_name=m.external_name or "",
                       external_avatar=m.external_avatar or "",
                       access_token=m.access_token or "",
                       refresh_token=m.refresh_token or "",
                       token_expires_at=m.token_expires_at or "", status=m.status,
                       created_at=m.created_at or "",
                       metadata_json=m.metadata_json or "{}")


def _cred_dto(m: UserCredentialModel) -> UserCredentialDTO:
    return UserCredentialDTO(user_id=m.user_id, provider=m.provider,
                             config_json=m.config_json or "{}")


def _repo_dto(m: ManagedRepoModel) -> ManagedRepoDTO:
    return ManagedRepoDTO(repo_key=m.repo_key, repo_owner=m.repo_owner,
                          repo_name=m.repo_name, full_name=m.full_name,
                          webhook_id=m.webhook_id or 0, host_status=m.host_status or "",
                          host_user_id=m.host_user_id or 0,
                          webhook_secret=m.webhook_secret or "", org_id=m.org_id or "",
                          bot_token_status=m.bot_token_status or "")


class AccountRepository:
    def get_by_username(self, session: Session, username: str) -> "AccountDTO | None":
        m = session.execute(select(AccountModel).where(
            AccountModel.username == (username or "").lower())).scalar_one_or_none()
        return _account_dto(m) if m else None

    def get_by_id(self, session: Session, uid: str) -> "AccountDTO | None":
        m = session.get(AccountModel, str(uid))
        return _account_dto(m) if m else None


class IdentityRepository:
    def get_by_platform(self, session: Session, provider: str,
                        external_id: str) -> "IdentityDTO | None":
        m = session.execute(select(ExternalIdentityModel).where(
            ExternalIdentityModel.provider == provider,
            ExternalIdentityModel.external_id == str(external_id))).scalar_one_or_none()
        return _identity_dto(m) if m else None

    def list_by_user(self, session: Session, grape_user_id: str) -> "list[IdentityDTO]":
        rows = session.execute(select(ExternalIdentityModel).where(
            ExternalIdentityModel.grape_user_id == str(grape_user_id))).scalars().all()
        return [_identity_dto(m) for m in rows]

    def upsert_tokens(self, session: Session, grape_user_id: str, provider: str,
                      external_id: str, external_login: str, access_token: str,
                      external_name: str = "", refresh_token: str = "") -> int:
        """写凭据（幂等 upsert）：只 flush 不 commit（D53 唯一提交点=UoW 出口）。"""
        m = session.execute(select(ExternalIdentityModel).where(
            ExternalIdentityModel.provider == provider,
            ExternalIdentityModel.external_id == str(external_id))).scalar_one_or_none()
        if m is None:
            m = ExternalIdentityModel(id=str(__import__("uuid").uuid4()),
                                      grape_user_id=str(grape_user_id), provider=provider,
                                      external_id=str(external_id))
            session.add(m)
        m.external_login = external_login
        m.external_name = external_name or (m.external_name or "")
        m.access_token = access_token
        if refresh_token:
            m.refresh_token = refresh_token
        m.updated_at = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session.flush()
        return m.id


class UserCredentialRepository:
    def get(self, session: Session, user_id: str, provider: str) -> "UserCredentialDTO | None":
        m = session.query(UserCredentialModel).filter_by(
            user_id=str(user_id), provider=provider).first()  # D105：id 列入 PK 后不再用复合 get
        return _cred_dto(m) if m else None

    def set(self, session: Session, user_id: str, provider: str, config_json: str) -> None:
        m = session.query(UserCredentialModel).filter_by(
            user_id=str(user_id), provider=provider).first()
        if m is None:
            from src.id_gen import uuid7_str  # D105：新行补 UUID 主键
            m = UserCredentialModel(id=uuid7_str(), user_id=str(user_id), provider=provider)
            session.add(m)
        m.config_json = config_json or "{}"
        m.updated_at = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session.flush()


class ManagedRepoRepository:
    def get(self, session: Session, repo_key: str) -> "ManagedRepoDTO | None":
        m = session.get(ManagedRepoModel, repo_key)
        return _repo_dto(m) if m else None

    def find_by_full_name_any_provider(self, session: Session,
                                       full_name: str) -> "list[ManagedRepoDTO]":
        """S81：按 full_name 反查任意 provider 前缀的托管行
        （等价 managed_repos.repo_key LIKE '%:full_name'，供
        webhook_secret 反查/repair-host provider 反查等通用面）。"""
        rows = session.execute(select(ManagedRepoModel).where(
            ManagedRepoModel.repo_key.like("%:" + full_name))).scalars().all()
        return [_repo_dto(m) for m in rows]

    def exists(self, session: Session, repo_key: str) -> bool:
        return session.get(ManagedRepoModel, repo_key) is not None

    def get_webhook_secret(self, session: Session, repo_key: str) -> str:
        m = session.get(ManagedRepoModel, repo_key)
        return (m.webhook_secret or "") if m else ""

    def list_by_prefix(self, session: Session, full_name: str) -> "list[ManagedRepoDTO]":
        rows = session.execute(select(ManagedRepoModel).where(
            ManagedRepoModel.repo_key.like("%:" + full_name))).scalars().all()
        return [_repo_dto(m) for m in rows]




class IdentityAdminRepository:
    """身份域管理面读写（S79）：DTO 携带**密文**（cipher 字段），加解密/
    状态机语义留在 grape_account_db（Service 层）——与裸 SQL 路径逐字节
    对齐。status 语义：1=活跃 0=解绑 4=token 过期。"""

    @staticmethod
    def get_by_platform(session: Session, provider: str, external_id: str,
                        statuses: "tuple[int, ...]" = (1,)) -> "IdentityDTO | None":
        m = session.execute(select(ExternalIdentityModel).where(
            ExternalIdentityModel.provider == provider,
            ExternalIdentityModel.external_id == str(external_id),
            ExternalIdentityModel.status.in_(statuses))).scalar_one_or_none()
        return _identity_dto(m) if m else None

    @staticmethod
    def get_by_platform_any_status(session: Session, provider: str,
                                   external_id: str) -> "IdentityDTO | None":
        m = session.execute(select(ExternalIdentityModel).where(
            ExternalIdentityModel.provider == provider,
            ExternalIdentityModel.external_id == str(external_id))).scalar_one_or_none()
        return _identity_dto(m) if m else None

    @staticmethod
    def get_active_by_user_provider(session: Session, grape_user_id: str,
                                    provider: str) -> "IdentityDTO | None":
        m = session.execute(select(ExternalIdentityModel).where(
            ExternalIdentityModel.grape_user_id == str(grape_user_id),
            ExternalIdentityModel.provider == provider,
            ExternalIdentityModel.status == 1)).scalar_one_or_none()
        return _identity_dto(m) if m else None

    @staticmethod
    def list_by_user_statuses(session: Session, grape_user_id: str,
                              statuses: "tuple[int, ...]" = (1, 4)) -> "list[IdentityDTO]":
        rows = session.execute(select(ExternalIdentityModel).where(
            ExternalIdentityModel.grape_user_id == str(grape_user_id),
            ExternalIdentityModel.status.in_(statuses))).scalars().all()
        return [_identity_dto(m) for m in rows]

    @staticmethod
    def update_by_id(session: Session, identity_id: str, external_id: str,
                     external_login: str, external_name: str,
                     external_avatar: str, access_token_cipher: str,
                     refresh_token_cipher: str, token_expires_at: str,
                     updated_at: str, status: int) -> None:
        m = session.get(ExternalIdentityModel, str(identity_id))  # D105：主键已 UUID/字符串化
        if m is None:
            raise ValueError(f"external_identities id={identity_id} not found")
        m.external_id = external_id
        m.external_login = external_login
        m.external_name = external_name
        m.external_avatar = external_avatar
        m.access_token = access_token_cipher
        m.refresh_token = refresh_token_cipher
        m.token_expires_at = token_expires_at
        m.updated_at = updated_at
        m.status = status
        session.flush()

    @staticmethod
    def create(session: Session, grape_user_id: str, provider: str,
               external_id: str, external_login: str, external_name: str,
               external_avatar: str, access_token_cipher: str,
               refresh_token_cipher: str, token_expires_at: str,
               status: int, created_at: str) -> str:
        import uuid as _uuid  # D105：主键 UUID 应用层生成
        m = ExternalIdentityModel(id=_uuid7(), grape_user_id=str(grape_user_id), provider=provider,
                                  external_id=str(external_id), external_login=external_login,
                                  external_name=external_name, external_avatar=external_avatar,
                                  access_token=access_token_cipher,
                                  refresh_token=refresh_token_cipher,
                                  token_expires_at=token_expires_at, status=status,
                                  created_at=created_at, updated_at=created_at)
        session.add(m)
        session.flush()
        return m.id


identity_admin_repository = IdentityAdminRepository()


account_repository = AccountRepository()
identity_repository = IdentityRepository()
user_credential_repository = UserCredentialRepository()
managed_repo_repository = ManagedRepoRepository()


class RepoCredentialRepository:
    """仓库凭据单表 CRUD（S80/D55）：双写期与 managed_repos 列共存。"""

    @staticmethod
    def get(session: Session, repo_key: str, cred_type: str) -> "str | None":
        # D99：主键=id——复合唯一键查询走条件式（session.get 需主键序）
        m = session.query(RepoCredentialModel).filter_by(
            repo_key=repo_key, cred_type=cred_type).first()
        return (m.value or "") if m else None

    @staticmethod
    def set(session: Session, repo_key: str, cred_type: str, value: str) -> None:
        # D99：主键=id——UPSERT 语义：有行更新 value；无行生成 id 插入
        m = session.query(RepoCredentialModel).filter_by(
            repo_key=repo_key, cred_type=cred_type).first()
        if m is None:
            import uuid as _uuid
            m = RepoCredentialModel(id=_uuid7(), repo_key=repo_key,
                                    cred_type=cred_type)
            session.add(m)
        m.value = value or ""
        m.updated_at = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session.flush()

    @staticmethod
    def delete(session: Session, repo_key: str, cred_type: str) -> None:
        # D99：主键=id——删除走条件式（原复合元组 session.get 在新主键下失效）
        m = session.query(RepoCredentialModel).filter_by(
            repo_key=repo_key, cred_type=cred_type).first()
        if m is not None:
            session.delete(m)
            session.flush()

    @staticmethod
    def list_by_repo(session: Session, repo_key: str) -> list:
        rows = session.execute(select(RepoCredentialModel).where(
            RepoCredentialModel.repo_key == repo_key)).scalars().all()
        from src.persistence.dto import RepoCredentialDTO
        return [RepoCredentialDTO(repo_key=m.repo_key, cred_type=m.cred_type,
                                  value=m.value or "") for m in rows]


repo_credential_repository = RepoCredentialRepository()


class SystemCredentialRepository:
    """D100 批 1：grape_t_system_credentials ORM CRUD（铁律 9：常规 CRUD 走 ORM）。

    【内部组件】仅供门面 src/domains/mgmt/user/grape_t_system_credentials.py 使用——本仓库类
    不做加密/审计/权限（那是门面的职责分层），只管行存取。直接 import
    本仓库绕过门面 = 错用（拿到密文 + 零审计），DTO docstring 已声明该
    失败形态。
    upsert/get 形态与 UserPlatformCredentialRepository 同构（get→分支更新，
    显式控制冲突策略，不依赖方言特定语法）。
    """

    @staticmethod
    def get(session, scope_type: str, scope_id: str, cred_type: str) -> "object | None":  # noqa: A002
        m = session.query(SystemCredentialModel).filter_by(
            scope_type=scope_type, scope_id=scope_id, cred_type=cred_type).first()
        return m

    @staticmethod
    def list_by_type(session, cred_type: str) -> "list[object]":
        """按 cred_type 列出（管理面/批 2 迁移探针路径；idx_sc_cred_type 覆盖）。"""
        return session.query(SystemCredentialModel).filter_by(
            cred_type=cred_type).all()

    @staticmethod
    def insert(session, scope_type: str, scope_id: str, cred_type: str,
               encrypted_value: str) -> str:
        """新建行（应用层生成 UUID；时间列门面已赋值后传入）。

        UNIQUE 冲突直接抛 IntegrityError——由门面转为"已存在"语义错误，
        本层不做 upsert 混 semantics（create 与 update 是两个显式入口）。
        """
        import uuid
        m = SystemCredentialModel(
            id=_uuid7(), scope_type=scope_type, scope_id=scope_id,
            cred_type=cred_type, value=encrypted_value,
            created_at=_now_str(), updated_at=_now_str())
        session.add(m)
        session.flush()
        return m.id

    @staticmethod
    def update_value(session, scope_type: str, scope_id: str, cred_type: str,
                     encrypted_value: str) -> bool:
        """更新 value（id/created_at 不动——审计锚点稳定）。返回行是否存在。"""
        m = session.query(SystemCredentialModel).filter_by(
            scope_type=scope_type, scope_id=scope_id, cred_type=cred_type).first()
        if m is None:
            return False
        m.value = encrypted_value
        m.updated_at = _now_str()
        session.flush()
        return True

    @staticmethod
    def delete(session, scope_type: str, scope_id: str, cred_type: str) -> bool:
        """物理删行（方案稿 §1.3 软删除裁定：删除即失效，审计靠 usage_log
        历史 credential_id 悬空定位——审计只增不改）。返回行是否存在。"""
        m = SystemCredentialRepository.get(session, scope_type, scope_id, cred_type)
        if m is None:
            return False
        session.delete(m)
        session.flush()
        return True


def _now_str() -> str:
    """门面与仓库共用的时间戳（应用层维护，铁律 2；%Y-%m-%d %H:%M:%S
    与全库时间列格式一致——字典序即时间序，rc/usg_log 同款）。"""
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


system_credential_repository = SystemCredentialRepository()
