"""D109 能力视图（派生读模型，不落表）。

实查口径（d109 §4）：
- `hosting`：实时查 provider 模块 `HOSTING_FULL_FLOW`（**不落表**，代码声明就是真源；
  落表会让 config_hash 语义崩坏——代码改了文件没变，表里旧值无法漂移检测）；
- `configured`：`grape_t_repo_platforms` 行存在且 enabled=1；
- `login` / `login_supported`：`grape_t_oauth_channels` 行存在（第一批两者恒等）；
- 凭据能力：`drivers` 数组 + supports_bot_token / supports_webhook_secret / bot_username；
- `bot_accounts` 摘要来自**平台行**（D110：一平台一个 bot），只含 username / is_default / status / last_used_at，**不含 token 值或前缀**。

**表未就绪回落**（§16 影响 #1）：任何表读取失败或 `importer.is_table_backed()` 为假时，
本模块退回旧判据（平台卡 + provider 模块），保证登录/托管不被新表拖挂。
"""
from __future__ import annotations

from typing import Iterable

from src.kernel.config import log


def _hosting_from_modules() -> frozenset:
    """旧判据原样（repos.py `_hosting_implemented_providers`）——对拍基准。"""
    from src.domains.runtime.public import hosting_full_flow_providers

    return frozenset(hosting_full_flow_providers())


def hosting_providers() -> frozenset:
    """能走托管全流程的平台集合（代码声明，无配置来源）。"""
    return _hosting_from_modules()


def _cards() -> set:
    try:
        from src.domains.runtime.public import list_vcs_platforms

        return set(list_vcs_platforms())
    except Exception as e:  # 平台卡不可用时退回模块集合
        log.warning("capabilities: platform cards unavailable: %s", e)
        return set()


def _login_providers() -> set:
    """登录渠道集合（含纯登录渠道，如 huawei）。

    表就绪时表里就是权威，但 provider 集合必须**先在回落路径里也算出来**：
    否则 MySQL 后端（第一批不建表）下能力视图只剩托管平台，登录渠道整块消失
    （d109 §11：huawei 应为 login=true / configured=false）。
    """
    try:
        from src.domains.mgmt.public import list_providers

        return set(list_providers())
    except Exception as e:
        log.warning("capabilities: login channels unavailable: %s", e)
        return set()


def known_providers() -> set:
    """已知平台集合 = 存量三平台 ∪ 平台卡 ∪ 登录渠道 ∪ 表内平台（表就绪时）。"""
    known = {"gitcode", "github", "gitlab"} | _cards() | _login_providers()
    try:
        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.models_platform_object import RepoPlatformModel
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        with Session(get_engine()) as s:
            known |= {r.provider for r in s.execute(select(RepoPlatformModel.provider)).all()}
    except Exception as e:
        log.debug(f"platform capabilities: platform table read failed, card-only set: {e}")
    return known


def _rows():
    """读表（就绪时）；返回 (platforms, channels) 或 None。"""
    try:
        from src.domains.platform.importer import is_table_backed

        if not is_table_backed():
            return None
        from sqlalchemy.orm import Session
        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import (
            OAuthChannelRepo,
            RepoPlatformRepo,
        )

        with Session(get_engine()) as s:
            return (
                list(RepoPlatformRepo.list_all(s, enabled_only=True)),
                list(OAuthChannelRepo.list_all(s, enabled_only=True)),
            )
    except Exception as e:
        log.warning("capabilities: table read failed, fallback to legacy: %s", e)
        return None


def platform_capabilities(provider: str) -> dict:
    """单平台能力视图（字段见 d109 §4；bot_accounts 摘要不含 token）。"""
    hosting = hosting_providers()
    rows = _rows()
    caps_by_provider = {}
    login_set: set[str] = set()
    login = provider in _login_providers()  # 回落判据：渠道文件/meta（表未就绪时唯一来源）
    configured = provider in _cards()  # 回落判据
    enabled = True  # 表未就绪时按"在册即可用"回落（与旧判据一致）
    if rows is not None:
        platforms, channels = rows
        configured = any(p.provider == provider for p in platforms)
        enabled = any(p.provider == provider and bool(p.enabled) for p in platforms)
        login_set = {c.provider for c in channels}
        login = provider in login_set  # 表就绪时表为权威
    # 2026-11-14：drivers 概念整体删除（凭据种类不再声明；PAT 与 OAuth 是并存模式，无需清单）
    # D110：bot 归平台行（一平台一个）；摘要仍是列表形态（前端/老调用方兼容）
    bot_accounts = _bot_summary(provider)
    return {
        "provider": provider,
        "login": login,
        "login_supported": login,  # 第一批恒等（留扩展位）
        "configured": configured,
        "enabled": enabled,  # 2026-11-14：区分"卡不存在"与"被管理面禁用"（两者 configured 都可能 False）
        "hosting": provider in hosting,
        "supports_bot_token": bool(bot_accounts),  # D110：平台行有 bot（username 或 token）即支持
        "supports_webhook_secret": False,          # secret 仍按仓库级凭据管理
        "bot_username": (bot_accounts[0]["username"] if bot_accounts else ""),
        "bot_accounts": bot_accounts,
    }


def _bot_summary(provider: str) -> list[dict]:
    """平台 bot 摘要 —— 来源是平台行（D110），不含 token 值与前缀。

    返回**列表形态**仅为兼容既有前端/调用方（老结构是多账号目录）；D110 后一平台一个 bot，
    所以最多一行：`is_default` 恒 true，`status` 由"是否有 token"派生（有=active，无=disabled），
    `last_used_at` 回巡检时间（bot_checked_at），不是"最近使用"——真正使用时间没有落点。
    """
    try:
        from sqlalchemy.orm import Session
        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

        with Session(get_engine()) as s:
            row = RepoPlatformRepo.get(s, provider)
            if row is None:
                return []
            username = getattr(row, "bot_username", "") or ""
            token_present = bool(getattr(row, "bot_token_enc", "") or "")
            if not username and not token_present:
                return []
            return [
                {
                    "username": username,
                    "is_default": True,   # D110：一平台一个 bot，恒为该平台默认身份
                    "status": ("active" if token_present else "disabled"),
                    "last_used_at": getattr(row, "bot_checked_at", "") or "",
                }
            ]
    except Exception:
        return []


def user_platform_view(user_id: str) -> dict:
    """用户维度的平台可用性视图（派生，不落表）。

    关系给出"接入过哪些平台"，可用性由 credential_resolver 现算（PAT / 渠道令牌）。
    字段：linked / available / source / reason —— 不含任何 token。
    """
    from src.kernel.persistence.repositories_platform_object import (
        UserRepoPlatformLinkRepo,
    )
    from src.domains.platform.credential_resolver import resolve_user_credential

    out: dict[str, dict] = {}
    try:
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine

        with Session(get_engine()) as s:
            rows = list(UserRepoPlatformLinkRepo.list_by_user(s, str(user_id)))
    except Exception as e:
        log.warning("capabilities: user links read failed: %s", e)
        return {}
    for row in rows:
        p = str(row.repo_provider)
        cred = resolve_user_credential(str(user_id), p)
        # available = 关系有效 + 至少有一个凭据来源（PAT / 活跃渠道令牌）。
        # 与"实际取 token"保持一致（fetch_user_token ①PAT②OAuth 序不动），**不叠加 scope/audience 校验**；
        # 那部分只作为 diagnosis 呈现（配置是否声明齐全），避免展示层与实际行为不一致。
        out[p] = {
            "provider": p,
            "linked": not bool(row.revoked_at),
            "revoked_reason": row.revoked_reason or "",
            "available": bool(not row.revoked_at and cred.token),
            "source": cred.source,
            "diagnosis": {
                "declared_usable": cred.ok,   # 按 drivers/scope/audience 声明校验的预期结果（提示用）
                "reason": cred.reason,        # 空 = 声明层面也通过
                "detail": cred.detail,
            },
        }
    return out


def all_capabilities(providers: Iterable[str] | None = None) -> dict:
    """全平台视图（默认 known_providers()）。"""
    keys = sorted(set(providers) if providers else known_providers())
    return {p: platform_capabilities(p) for p in keys}