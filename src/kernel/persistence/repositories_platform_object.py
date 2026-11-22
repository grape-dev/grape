"""D109 平台对象模型 Repository（六聚合）。

纪律（d109 §4 / D53）：
- 方法接受外部 `Session`，**只 flush 不 commit**（提交点收敛在 UoW 出口）；
- 配置派生三表（channel / platform / caps）的 upsert 共用一套语义：
  `source=manual` 跳过、`source=orphaned` 恢复（无视 hash）、hash 相同跳过、
  写保护列（enabled / 凭据列）永不被配置覆盖；
- 运行态表（bot_accounts / user_platform_link）不参与导入；
- 位运算用 SQL 表达式（`via_bits | :bit`），不做读-改-写。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from src.kernel.config import log
from src.kernel.id_gen import uuid7_str
from src.kernel.persistence.models_platform_object import (
    ChannelRepoLinkModel,
    OAuthChannelModel,
    RepoPlatformModel,
    UserRepoPlatformLinkModel,
)

# 由 DB 管理、配置导入不得覆盖的列（enabled 归 DB；凭据列归管理面）
_PROTECTED = {"enabled", "client_id_enc", "client_secret_enc", "created_at",
              # D110：平台 bot 三件 + 名字（事实列，管理面/巡检写）
              "bot_token_enc", "bot_username", "bot_token_status", "bot_checked_at"}
# 其中允许"空值播种"的：配置/平台卡只在当前为空时填一次，之后永不覆盖
_SEEDABLE_PROTECTED = {"bot_token_enc", "bot_username"}
# 用户面默认不展示的平台（2026-11-15 用户裁定：写在初始化代码里，不进平台卡）。
# 语义：**只在新建平台行/恢复行时**给 enabled=0，之后 DB 权威（管理面开关），
# 导入永不覆盖既有行——所以既有实例升级不会把运维已经开着的平台静默关掉。
DEFAULT_HIDDEN_PLATFORMS = frozenset({"gitlab"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_COLUMN_CACHE: dict[str, set[str]] = {}


def _column_names(model) -> set[str]:
    """模型列名集合（缓存）。用于未知键 fail-closed——拼错字段名不许静默丢弃。"""
    name = model.__tablename__
    cols = _COLUMN_CACHE.get(name)
    if cols is None:
        cols = {c.key for c in model.__mapper__.columns}
        _COLUMN_CACHE[name] = cols
    return cols


def _default_enabled(provider: str) -> int:
    """新建平台行时的 enabled 默认值：默认不展示集合里的平台给 0。"""
    return 0 if str(provider) in DEFAULT_HIDDEN_PLATFORMS else 1


def _apply_fields(row, fields: dict) -> None:
    cols = _column_names(type(row))
    for key, value in fields.items():
        if key in _PROTECTED:
            # D110 播种规则：白名单内的写保护列仅在"当前为空"时接受配置值，
            # 之后导入永不覆盖（否则管理面写的 bot 名/凭据会被下次导入清掉）；
            # enabled/created_at 等其余保护列维持硬跳过（0 也是有效状态，不能被配置复位）。
            if key in _SEEDABLE_PROTECTED and not (getattr(row, key, "") or ""):
                setattr(row, key, value)
            continue
        if key not in cols:
            raise ValueError(f"未知配置键 {key!r}（表 {row.__tablename__}, provider={row.provider}）——整条拒绝")
        setattr(row, key, value)


def _upsert_config_row(
    session: Session,
    model,
    provider: str,
    fields: dict,
    config_hash: str,
    *,
    default_source: str = "config",
) -> str:
    """配置派生行的统一导入语义。返回动作：inserted/updated/skipped/restored/manual_kept。"""
    row = session.execute(
        select(model).where(model.provider == provider)  # type: ignore[attr-defined]
    ).scalar_one_or_none()
    now = _now()
    if row is None:
        row = model(id=uuid7_str(), provider=provider, source=default_source, config_hash=config_hash)
        if "enabled" in getattr(model, "__table__").columns:
            row.enabled = _default_enabled(provider)
        _apply_fields(row, fields)
        session.add(row)
        session.flush()
        return "inserted"
    if row.source == "manual":
        return "manual_kept"
    if row.source == "orphaned":
        # 配置删了又加回：无视 hash 直接恢复（d109 §8）
        row.source = default_source
        # 恢复行 = 配置里又有它了 → 视为可用（与旧语义一致）。"默认不展示"
        # 只在**新建行**与迁移时落一次，不在恢复路径重算——否则"改卡/挪文件"会把
        # 运维在管理面开过的平台莫名其妙关掉。
        row.enabled = 1
        row.config_hash = config_hash
        _apply_fields(row, fields)
        row.updated_at = now
        session.flush()
        return "restored"
    if (row.config_hash or "") == config_hash:
        return "skipped"
    _apply_fields(row, fields)
    row.config_hash = config_hash
    row.updated_at = now
    session.flush()
    return "updated"


def _mark_orphaned(session: Session, model, present_providers: Iterable[str]) -> list[str]:
    """文件里没有的行 → orphaned + enabled=0（软删，不级联）。"""
    present = set(present_providers)
    rows = session.execute(select(model)).scalars().all()
    changed: list[str] = []
    for row in rows:
        if row.source == "manual" or row.provider in present:
            continue
        if row.source != "orphaned":
            row.source = "orphaned"
            row.enabled = 0
            row.updated_at = _now()
            changed.append(row.provider)
    session.flush()
    return changed


class OAuthChannelRepo:
    """登录渠道（配置派生 + 加密客户端凭据）。"""

    model = OAuthChannelModel

    @staticmethod
    def get(session: Session, provider: str) -> OAuthChannelModel | None:
        return session.execute(
            select(OAuthChannelModel).where(OAuthChannelModel.provider == provider)
        ).scalar_one_or_none()

    @staticmethod
    def list_all(session: Session, *, enabled_only: bool = False) -> Sequence[OAuthChannelModel]:
        stmt = select(OAuthChannelModel).order_by(OAuthChannelModel.provider)
        if enabled_only:
            stmt = stmt.where(OAuthChannelModel.enabled == 1)
        return session.execute(stmt).scalars().all()

    @staticmethod
    def upsert_from_config(session: Session, provider: str, fields: dict, config_hash: str) -> str:
        if "repo_links" in fields:
            fields = dict(fields)
            fields["repo_links_json"] = json.dumps(
                fields.pop("repo_links"), ensure_ascii=False, sort_keys=True
            )
        return _upsert_config_row(session, OAuthChannelModel, provider, fields, config_hash)

    @staticmethod
    def mark_orphaned(session: Session, present_providers: Iterable[str]) -> list[str]:
        return _mark_orphaned(session, OAuthChannelModel, present_providers)

    @staticmethod
    def set_enabled(session: Session, provider: str, enabled: bool) -> bool:
        res = session.execute(
            update(OAuthChannelModel)
            .where(OAuthChannelModel.provider == provider)
            .values(enabled=1 if enabled else 0, updated_at=_now())
        )
        return bool(getattr(res, "rowcount", 0))

    @staticmethod
    def set_client_credentials(session: Session, provider: str, client_id_enc: str, client_secret_enc: str) -> bool:
        """凭据列只由管理面/迁移写（D109 决策 A：meta → 表）。值已是密文。"""
        res = session.execute(
            update(OAuthChannelModel)
            .where(OAuthChannelModel.provider == provider)
            .values(
                client_id_enc=client_id_enc,
                client_secret_enc=client_secret_enc,
                updated_at=_now(),
            )
        )
        return bool(getattr(res, "rowcount", 0))


class RepoPlatformRepo:
    """仓库平台方言声明。"""

    model = RepoPlatformModel

    @staticmethod
    def get(session: Session, provider: str) -> RepoPlatformModel | None:
        return session.execute(
            select(RepoPlatformModel).where(RepoPlatformModel.provider == provider)
        ).scalar_one_or_none()

    @staticmethod
    def list_all(session: Session, *, enabled_only: bool = False) -> Sequence[RepoPlatformModel]:
        stmt = select(RepoPlatformModel).order_by(RepoPlatformModel.provider)
        if enabled_only:
            stmt = stmt.where(RepoPlatformModel.enabled == 1)
        return session.execute(stmt).scalars().all()

    @staticmethod
    def upsert_from_config(session: Session, provider: str, fields: dict, config_hash: str) -> str:
        return _upsert_config_row(session, RepoPlatformModel, provider, fields, config_hash)

    @staticmethod
    def upsert_manual(session: Session, provider: str, fields: dict) -> str:
        """管理面新增/编辑「手建实例行」（Issue #3580 复审）。

        与 `upsert_from_config` 的差异：
        - 新建行 `source='manual'`：导入器永不覆盖（`_upsert_config_row` 见 manual 即
          `manual_kept`），`mark_orphaned` 也不会把它软删；
        - 已存在 manual 行 → 更新定义字段（`_PROTECTED` 里的 enabled / bot 三件照旧跳过）；
        - 已存在 config / orphaned 行（来自 `configs/platforms/<p>.json`）→ 返回
          `config_owned` 且**不写**：该 provider 的定义真源是配置文件，管理面改它
          会造双真源（下次启动导入即被冲回）。

        返回动作：inserted | updated | config_owned。
        """
        row = session.execute(
            select(RepoPlatformModel).where(RepoPlatformModel.provider == provider)
        ).scalar_one_or_none()
        if row is None:
            row = RepoPlatformModel(id=uuid7_str(), provider=provider,
                                    source="manual", config_hash="")
            setattr(row, "enabled", _default_enabled(provider))
            _apply_fields(row, fields)
            session.add(row)
            session.flush()
            return "inserted"
        if str(getattr(row, "source", "") or "") != "manual":
            return "config_owned"
        _apply_fields(row, fields)
        setattr(row, "updated_at", _now())
        session.flush()
        return "updated"

    @staticmethod
    def mark_orphaned(session: Session, present_providers: Iterable[str]) -> list[str]:
        return _mark_orphaned(session, RepoPlatformModel, present_providers)

    @staticmethod
    def set_enabled(session: Session, provider: str, enabled: bool) -> bool:
        res = session.execute(
            update(RepoPlatformModel)
            .where(RepoPlatformModel.provider == provider)
            .values(enabled=1 if enabled else 0, updated_at=_now())
        )
        return bool(getattr(res, "rowcount", 0))

    # ---- D110：平台唯一 bot（一平台一个；空 token=该平台无全局 bot）----

    @staticmethod
    def get_bot_token_enc(session: Session, provider: str) -> str:
        """平台 bot token 密文（未解密；空串=未配置）。调用方负责解密。"""
        row = RepoPlatformRepo.get(session, provider)
        return (getattr(row, "bot_token_enc", "") or "") if row is not None else ""

    @staticmethod
    def get_bot(provider: str) -> dict:
        """平台 bot 概要（不含 token 值；读不到返回空 dict）。"""
        try:
            from sqlalchemy.orm import Session

            from src.kernel.persistence.engine import get_engine

            with Session(get_engine()) as s:
                row = RepoPlatformRepo.get(s, provider)
                if row is None:
                    return {}
                return {
                    "provider": row.provider,
                    "username": getattr(row, "bot_username", "") or "",
                    "token_present": bool(getattr(row, "bot_token_enc", "") or ""),
                    "token_status": getattr(row, "bot_token_status", "") or "unknown",
                    "checked_at": getattr(row, "bot_checked_at", "") or "",
                }
        except Exception:
            return {}

    @staticmethod
    def set_bot(session: Session, provider: str, *, username: str | None = None,
                token_enc: str | None = None) -> bool:
        """管理面写平台 bot：username/token_enc 为 None 表示不改；token_enc='' 表示清除。"""
        values = {"updated_at": _now()}
        if username is not None:
            values["bot_username"] = username
        if token_enc is not None:
            values["bot_token_enc"] = token_enc
            values["bot_token_status"] = "unknown" if token_enc else ""
        res = session.execute(
            update(RepoPlatformModel).where(RepoPlatformModel.provider == provider).values(**values)
        )
        return bool(getattr(res, "rowcount", 0))

    @staticmethod
    def mark_bot_status(session: Session, provider: str, token_status: str, checked_at: str = "") -> bool:
        res = session.execute(
            update(RepoPlatformModel)
            .where(RepoPlatformModel.provider == provider)
            .values(bot_token_status=token_status, bot_checked_at=checked_at or _now(),
                    updated_at=_now())
        )
        return bool(getattr(res, "rowcount", 0))



class ChannelRepoLinkRepo:
    """渠道→仓库平台授权声明。显式声明与缺省派生同表，用 source 区分。"""

    @staticmethod
    def get(session: Session, oauth_provider: str, repo_provider: str) -> "ChannelRepoLinkModel | None":
        """单条授权关系（credential_resolver 用：渠道令牌能否驱动该平台）。"""
        return session.execute(
            select(ChannelRepoLinkModel).where(
                ChannelRepoLinkModel.oauth_provider == oauth_provider,
                ChannelRepoLinkModel.repo_provider == repo_provider,
            )
        ).scalar_one_or_none()

    @staticmethod
    def list_by_oauth(session: Session, oauth_provider: str) -> Sequence[ChannelRepoLinkModel]:
        return session.execute(
            select(ChannelRepoLinkModel)
            .where(ChannelRepoLinkModel.oauth_provider == oauth_provider)
            .order_by(ChannelRepoLinkModel.repo_provider)
        ).scalars().all()

    @staticmethod
    def list_by_repo(session: Session, repo_provider: str) -> Sequence[ChannelRepoLinkModel]:
        return session.execute(
            select(ChannelRepoLinkModel).where(ChannelRepoLinkModel.repo_provider == repo_provider)
        ).scalars().all()

    @staticmethod
    def set_manual(session: Session, oauth_provider: str, repo_provider: str) -> None:
        """管理面写关系 → source=manual（DB 权威：此后导入不覆盖它）。"""
        now = _now()
        row = ChannelRepoLinkRepo.get(session, oauth_provider, repo_provider)
        if row is not None:
            row.source = "manual"
            row.updated_at = now
            session.flush()
            return
        session.add(
            ChannelRepoLinkModel(
                id=uuid7_str(),
                oauth_provider=oauth_provider,
                repo_provider=repo_provider,

                source="manual",
                config_hash="",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()

    @staticmethod
    def delete_manual(session: Session, oauth_provider: str, repo_provider: str) -> bool:
        """只删 manual 行（config/derived 行归导入管理，管理面不得删）。"""
        row = ChannelRepoLinkRepo.get(session, oauth_provider, repo_provider)
        if row is None or str(row.source) != "manual":
            return False
        session.delete(row)
        session.flush()
        return True

    @staticmethod
    def merge_for_oauth(session: Session, oauth_provider: str, links: Sequence[dict],
                        config_hash: str = "") -> int:
        """合并导入（2026-11-14 裁定：link 以 DB 为权威，配置是种子）。

        规则：config/derived 行按配置全量替换（文件删行即清理）；**manual 行保留且优先**
        —— 同一 (channel, repo) 已有 manual 行时不再插入配置行（避免唯一键冲突与覆盖）。
        返回本次按配置插入/更新的行数。
        """
        manual = {
            str(l.repo_provider)
            for l in session.execute(
                select(ChannelRepoLinkModel).where(
                    ChannelRepoLinkModel.oauth_provider == oauth_provider,
                    ChannelRepoLinkModel.source == "manual",
                )
            ).scalars().all()
        }
        session.execute(
            delete(ChannelRepoLinkModel).where(
                ChannelRepoLinkModel.oauth_provider == oauth_provider,
                ChannelRepoLinkModel.source != "manual",
            )
        )
        now = _now()
        n = 0
        for link in links:
            rp = str(link.get("repo_provider") or "")
            if rp in manual:  # DB 权威：manual 行不被配置覆盖
                continue
            session.add(
                ChannelRepoLinkModel(
                    id=uuid7_str(),
                    oauth_provider=oauth_provider,
                    repo_provider=rp,
                    source=str(link.get("source") or "config"),
                    config_hash=config_hash,
                    created_at=now,
                    updated_at=now,
                )
            )
            n += 1
        session.flush()
        return n


def _expire_loaded_link(session: Session, user_id: str, repo_provider: str) -> None:
    """P1-4：方言 upsert（INSERT…ON CONFLICT / ON DUPLICATE KEY）不经 ORM 同步，
    同 session 内已加载的实例会保留陈旧 revoked_at。这里只过期该行实例（不是
    expire_all）——避免波及调用方同 session 里的其它脏对象。
    """
    try:
        row = session.execute(
            select(UserRepoPlatformLinkModel).where(
                UserRepoPlatformLinkModel.user_id == user_id,
                UserRepoPlatformLinkModel.repo_provider == repo_provider,
            )
        ).scalar_one_or_none()
        if row is not None:
            session.expire(row)
    except Exception as e:  # 过期失败不阻断建关系（语义同 fail-open）
        log.warning("_expire_loaded_link failed (%s/%s): %s", user_id, repo_provider, e)


class UserRepoPlatformLinkRepo:
    """用户↔仓库平台的**纯对应关系**（2026-11-14 裁定）。

    只做 establish / revoke 两件事；不存来源、不存状态。可用性由
    `src.domains.platform.credential_resolver` 现算。
    """

    @staticmethod
    def get(session: Session, user_id: str, repo_provider: str) -> "UserRepoPlatformLinkModel | None":
        return session.execute(
            select(UserRepoPlatformLinkModel).where(
                UserRepoPlatformLinkModel.user_id == str(user_id),
                UserRepoPlatformLinkModel.repo_provider == repo_provider,
            )
        ).scalar_one_or_none()

    @staticmethod
    def list_by_user(session: Session, user_id: str) -> Sequence["UserRepoPlatformLinkModel"]:
        return session.execute(
            select(UserRepoPlatformLinkModel)
            .where(UserRepoPlatformLinkModel.user_id == str(user_id))
            .order_by(UserRepoPlatformLinkModel.repo_provider)
        ).scalars().all()

    @staticmethod
    def list_active_by_user(session: Session, user_id: str) -> Sequence["UserRepoPlatformLinkModel"]:
        return session.execute(
            select(UserRepoPlatformLinkModel).where(
                UserRepoPlatformLinkModel.user_id == str(user_id),
                UserRepoPlatformLinkModel.revoked_at == "",
            )
        ).scalars().all()

    @staticmethod
    def link(session: Session, user_id: str, repo_provider: str) -> None:
        """建立/恢复关系（幂等；任何来源重新获得即复活）。

        原实现是「UPDATE 命中就返回，否则 INSERT」两步：并发窗口里两个会话都可能
        UPDATE 0 行后各插一行 → 唯一键 (user_id, repo_provider) 冲突，异常被 hooks
        的 except 吞成 warning，外层 uow 一起回滚（同 uow 里的撤销/审计也丢）。
        改为方言级 upsert（SQLite ON CONFLICT / MySQL ON DUPLICATE KEY UPDATE），
        单语句完成，冲突即复活。未知方言回退两步（保持原语义，仅作兜底）。
        """
        now = _now()
        uid = str(user_id)
        vals = {
            "revoked_at": "", "revoked_reason": "", "updated_at": now,
            "linked_at": now, "created_at": now,
        }
        dialect = getattr(getattr(session, "bind", None), "dialect", None)
        name = getattr(dialect, "name", "")
        try:
            if name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as _ins

                stmt = _ins(UserRepoPlatformLinkModel).values(
                    id=uuid7_str(), user_id=uid, repo_provider=repo_provider, **vals)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["user_id", "repo_provider"],
                    set_={k: vals[k] for k in ("revoked_at", "revoked_reason", "updated_at")},
                )
                session.execute(stmt)
                session.flush()
                _expire_loaded_link(session, uid, repo_provider)
                return
            if name in ("mysql", "mariadb"):
                from sqlalchemy.dialects.mysql import insert as _ins

                stmt = _ins(UserRepoPlatformLinkModel).values(
                    id=uuid7_str(), user_id=uid, repo_provider=repo_provider, **vals)
                stmt = stmt.on_duplicate_key_update(
                    revoked_at="", revoked_reason="", updated_at=now)
                session.execute(stmt)
                session.flush()
                _expire_loaded_link(session, uid, repo_provider)
                return
        except Exception as e:  # upsert 不可用（方言/构造失败）→ 回落两步
            log.warning("link upsert 失败，回落 UPDATE/INSERT（非致命）: %s", e)

        res = session.execute(
            update(UserRepoPlatformLinkModel)
            .where(
                UserRepoPlatformLinkModel.user_id == uid,
                UserRepoPlatformLinkModel.repo_provider == repo_provider,
            )
            .values(revoked_at="", revoked_reason="", updated_at=now)
        )
        if getattr(res, "rowcount", 0):
            session.flush()
            return
        session.add(
            UserRepoPlatformLinkModel(
                id=uuid7_str(), user_id=uid, repo_provider=repo_provider,
                linked_at=now, created_at=now, updated_at=now,
            )
        )
        session.flush()

    @staticmethod
    def revoke(session: Session, user_id: str, repo_provider: str, reason: str) -> None:
        """撤销关系（行不存在则 no-op；幂等）。"""
        now = _now()
        session.execute(
            update(UserRepoPlatformLinkModel)
            .where(
                UserRepoPlatformLinkModel.user_id == str(user_id),
                UserRepoPlatformLinkModel.repo_provider == repo_provider,
            )
            .values(revoked_at=now, revoked_reason=reason, updated_at=now)
        )
        session.flush()

