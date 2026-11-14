"""仓库平台注册表读口（D109 对象模型优先，平台卡兜底）。

为什么需要这一层（2026-11-14 用户质询）：「查询系统支持哪些平台仓库」不应该再去
问 `/settings/vcs/platforms` 的卡驱动实现——平台已经是对象模型里的一等对象
（`grape_t_repo_platforms` 方言声明 + 同表的凭据能力列，PAT 创建页 URL 就在
`pat_create_url` 列；原 `grape_t_repo_credential_caps` 已于 2026-11-14 合表删除）。
卡只是配置文件，是**导入源**，不是运行时权威。

约定：
- `platform_descriptors()` 表优先：migrated 且表内有行 → 用表；否则回落平台卡
  （fail-open，与其余 D109 读路径同口径）；两者都不可用 → 空列表，调用方自处理。
- 返回字段与旧 `/settings/vcs/platforms` 保持一致（provider/name/host/dialect/
  brand_color/pat_create_url），前端不需要改字段语义。
- `supported_platforms()` 只给 id 列表（关系推导、平台可见性判定用）。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_DEFAULT_PAT_PATH = "/profile/personal_access_tokens"


def _descriptors_from_tables() -> "list[dict] | None":
    from src.domains.platform.importer import is_table_backed

    if not is_table_backed():
        return None

    from sqlalchemy.orm import Session

    from src.kernel.persistence.engine import get_engine
    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

    with Session(get_engine()) as s:
        rows = list(RepoPlatformRepo.list_all(s))
    if not rows:
        # 表里根本没有行 = 未迁移/空表 → None（允许调用方回退平台卡）
        return None

    out = []
    for r in rows:
        # 有行但 enabled=0 → 跳过并继续；若全部 skipped，out == [] 就是"用户面空目录"
        # 这个明确答案，绝不能退化成 None（否则会回退平台卡，把"全关"变成"全开"）。
        if not bool(getattr(r, "enabled", 1)):
            continue
        provider = str(getattr(r, "provider", "") or "")
        if not provider:
            continue
        host = str(getattr(r, "host", "") or "")
        pat_url = str(getattr(r, "pat_create_url", "") or "")
        if not pat_url and host:
            pat_url = f"https://{host}{_DEFAULT_PAT_PATH}"
        out.append({
            "provider": provider,
            "name": str(getattr(r, "name", "") or provider),
            "host": host,
            "dialect": str(getattr(r, "dialect", "") or ""),
            "brand_color": str(getattr(r, "brand_color", "") or ""),
            "pat_create_url": pat_url,
        })
    return out


def _descriptors_from_cards() -> "list[dict]":
    from src.domains.runtime.vcs.platform_card import get_platform_card, list_vcs_platforms

    out = []
    for provider in list_vcs_platforms():
        card = get_platform_card(provider)
        if card is None:
            continue
        out.append({
            "provider": provider,
            "name": card.name,
            "host": card.host,
            "dialect": card.dialect,
            "brand_color": card.brand_color,
            "pat_create_url": f"https://{card.host}{_DEFAULT_PAT_PATH}",
        })
    return out


def platform_descriptors() -> "list[dict]":
    """系统支持的仓库平台描述（表优先，卡兜底）。"""
    try:
        rows = _descriptors_from_tables()
    except Exception as e:
        log.warning(f"repo platform table read failed, fallback to cards: {e}")
        rows = None
    if rows is not None:   # 空列表也是答案（全部 disabled），只有 None 才回退
        return rows
    try:
        return _descriptors_from_cards()
    except Exception as e:
        log.warning(f"repo platform card read failed: {e}")
        return []


def supported_platforms() -> "list[str]":
    """系统支持的仓库平台 id 列表（对象模型优先）。"""
    return [str(d["provider"]) for d in platform_descriptors()]