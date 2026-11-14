"""D109 凭据迁移与关系回填（第一批块③，风险最高的一块）。

三项动作，全部幂等、可重跑，且**不物理删除 meta 旧值**（停读 + 保留一版，回滚可用；d109 §8 决策 A 修正）：

1. `migrate_oauth_client_credentials`：`meta oauth_<p>_client_id/_client_secret`
   + `oauth_providers_config` 卡内 client_id/client_secret → `grape_t_oauth_channels.client_id_enc/client_secret_enc`；
2. `migrate_platform_bots`（D110）：`meta global_bot_<p>_token` / 历史别名 `gitcode_api_token`|`github_bot_token` /
   `global_bot_<p>_username` → `grape_t_repo_platforms.bot_token_enc / bot_username`（一平台一个 bot，只补空位）；
3. `backfill_user_platform_links`：`user_credentials(kind=pat)` + 旧复合键 `<p>_pat` + `external_identities`
   按 `channel_repo_link` 展开 → `grape_t_user_repo_platform_link`（只写关系，不写来源位）。

对账口径（修正 §11 原措辞）：旧逻辑是「平台卡在册即全可见」（S107 ④，无用户维度），新逻辑按关系过滤，
**相对旧行为必然缩小——这是修缺陷的预期结果**，不是回归。所以对账不跟"旧的可见全集"比，而是校验
「每个用户有凭据/身份的平台 ⊆ 回填结果」，即 `reconcile_visible_platforms()` 的 missing 必须为空。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.kernel.config import log
from src.kernel.persistence.repositories_platform_object import (
    ChannelRepoLinkRepo,
    OAuthChannelRepo,
    RepoPlatformRepo,
    UserRepoPlatformLinkRepo,
)

_HISTORICAL_BOT_TOKEN_KEYS = {
    "gitcode": "gitcode_api_token",
    "github": "github_bot_token",
}
# 历史 username 别名（config_store._BOT_META_MAP：github_bot_username → global_bot_github_username）
_HISTORICAL_BOT_USERNAME_KEYS = {
    "github": "github_bot_username",
}
_PAT_SUFFIX = "_pat"


@dataclass
class MigrationStats:
    oauth_channels: int = 0
    platform_bots: int = 0
    pat_links: int = 0
    oauth_links: int = 0
    derived_from: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "oauth_channels": self.oauth_channels,
            "platform_bots": self.platform_bots,
            "pat_links": self.pat_links,
            "oauth_links": self.oauth_links,
            "derived_from": self.derived_from,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }


def _meta(key: str) -> str:
    from src.domains.mgmt.user.db import get_meta_value

    return (get_meta_value(key) or "").strip()


def _encrypt(value: str) -> str:
    from src.domains.mgmt.auth.credential_crypto import encrypt

    return encrypt(value) if value else ""


def _decrypt(value: str) -> str:
    from src.domains.mgmt.auth.credential_crypto import decrypt

    return decrypt(value) if value else ""


# ---------- 1. OAuth 客户端凭据：meta → 表加密列 ----------

def migrate_oauth_client_credentials(session: Session) -> int:
    """幂等：表已有 client_secret_enc 的渠道跳过（不覆盖管理面后来的写入）。"""
    n = 0
    for row in OAuthChannelRepo.list_all(session):
        if (row.client_secret_enc or "") or (row.client_id_enc or ""):
            continue
        provider = row.provider
        client_id = _meta(f"oauth_{provider}_client_id")
        client_secret = _meta(f"oauth_{provider}_client_secret")
        # 卡内凭据（oauth_providers_config JSON）兜底
        if not (client_id and client_secret):
            raw = _meta("oauth_providers_config")
            if raw:
                try:
                    card = (json.loads(raw) or {}).get(provider) or {}
                    client_id = client_id or str(card.get("client_id") or "")
                    client_secret = client_secret or str(card.get("client_secret") or "")
                except Exception as e:
                    log.warning("oauth_providers_config 解析失败（%s）: %s", provider, e)
        if not client_secret:
            continue
        OAuthChannelRepo.set_client_credentials(
            session, provider, _encrypt(client_id), _encrypt(client_secret)
        )
        n += 1
    return n


# ---------- 2. 平台 bot（D110：一平台一个 bot，写进平台行）----------

def migrate_platform_bots(session: Session, stats: MigrationStats | None = None) -> int:
    """幂等：平台行 bot 列为空时才写入（不覆盖管理面后来改的值）。

    D110 变更：bot 目录表已删除；这里只把 meta（含历史别名）搬进 grape_t_repo_platforms
    的空位，密文沿用 credential_crypto。
    """
    stats = stats or MigrationStats()
    created = 0
    providers = {r.provider for r in RepoPlatformRepo.list_all(session)}
    providers |= set(_HISTORICAL_BOT_TOKEN_KEYS)
    try:
        seen = session.execute(
            text("SELECT key FROM meta WHERE key LIKE 'global_bot_%'")
        ).fetchall()
        for (key,) in seen:
            if key.startswith("global_bot_"):
                rest = key[len("global_bot_"):]
                for suffix, cut in (("_token", 6), ("_username", 9)):
                    if rest.endswith(suffix):
                        providers.add(rest[: -cut])
    except Exception as e:
        # meta 扫描失败 → 少发现若干 provider，不阻断迁移（后续按平台卡补）
        log.debug(f"platform migration: meta provider scan failed: {e}")

    for provider in sorted(providers):
        row = RepoPlatformRepo.get(session, provider)
        if row is None:
            stats.skipped += 1
            continue
        token = _meta(f"global_bot_{provider}_token") or _meta(_HISTORICAL_BOT_TOKEN_KEYS.get(provider, ""))
        username = _meta(f"global_bot_{provider}_username") or _meta(_HISTORICAL_BOT_USERNAME_KEYS.get(provider, ""))
        token_enc = _encrypt(token) if token else ""
        changed = False
        if token_enc and not (row.bot_token_enc or ""):
            row.bot_token_enc = token_enc
            changed = True
        if username and not (row.bot_username or ""):
            row.bot_username = username
            changed = True
        if changed:
            created += 1
    session.flush()
    stats.platform_bots += created
    return created


# 兼容名：旧调用方/测试仍可能引用
migrate_bot_accounts = migrate_platform_bots


# ---------- 3. 用户关系回填 ----------

def _pat_pairs(session: Session) -> list[tuple[str, str]]:
    """(user_id, provider)：归位形态 kind='pat' + 双写期旧复合键 `<p>_pat`（kind=''）。"""
    pairs: dict[tuple[str, str], None] = {}
    for uid, prov, kind in session.execute(
        text("SELECT user_id, provider, COALESCE(kind,'') FROM user_credentials")
    ).fetchall():
        p = str(prov or "")
        k = str(kind or "")
        if k == "pat" and p:
            pairs[(str(uid), p)] = None
        elif not k and p.endswith(_PAT_SUFFIX):
            base = p[: -len(_PAT_SUFFIX)]
            if base:
                pairs[(str(uid), base)] = None
    return list(pairs)


def _oauth_pairs(session: Session) -> list[tuple[str, str]]:
    """(grape_user_id, oauth_provider)：未解绑且启用的身份。"""
    rows = session.execute(
        text(
            "SELECT grape_user_id, provider FROM external_identities "
            "WHERE COALESCE(unbound_at,'')='' AND COALESCE(status,1)=1"
        )
    ).fetchall()
    return [(str(u), str(p or "")) for u, p in rows if u and p]


def backfill_user_platform_links(session: Session) -> MigrationStats:
    stats = MigrationStats()
    platform_set = {r.provider for r in RepoPlatformRepo.list_all(session)}

    for uid, provider in _pat_pairs(session):
        if provider not in platform_set:
            stats.skipped += 1
            continue
        UserRepoPlatformLinkRepo.link(session, uid, provider)
        stats.pat_links += 1

    for uid, oauth_provider in _oauth_pairs(session):
        links = ChannelRepoLinkRepo.list_by_oauth(session, oauth_provider)
        targets = [str(l.repo_provider) for l in links]
        if not targets and oauth_provider in platform_set:
            targets = [oauth_provider]  # 缺省派生：自己是仓库平台才派生
        if not targets:
            stats.skipped += 1  # 纯登录渠道不产生幽灵关系
            continue
        for repo_provider in targets:
            if repo_provider not in platform_set:
                continue
            UserRepoPlatformLinkRepo.link(session, uid, repo_provider)
            stats.oauth_links += 1
    return stats


def reconcile_visible_platforms(session: Session) -> dict:
    """对账：期望集合（有 PAT / 有身份）必须 ⊆ 表内关系集合。missing 必须为空。"""
    platform_set = {r.provider for r in RepoPlatformRepo.list_all(session)}
    expected: set[tuple[str, str]] = set()
    for uid, provider in _pat_pairs(session):
        if provider in platform_set:
            expected.add((uid, provider))
    for uid, oauth_provider in _oauth_pairs(session):
        links = ChannelRepoLinkRepo.list_by_oauth(session, oauth_provider)
        targets = [str(l.repo_provider) for l in links] or (
            [oauth_provider] if oauth_provider in platform_set else []
        )
        for rp in targets:
            if rp in platform_set:
                expected.add((uid, rp))
    actual = {
        (str(r.user_id), str(r.repo_provider))
        for r in session.execute(
            text("SELECT user_id, repo_provider FROM grape_t_user_repo_platform_link")
        ).fetchall()
    }
    missing = sorted(expected - actual)
    return {
        "expected": len(expected),
        "actual": len(actual),
        "missing": missing[:50],
        "missing_count": len(missing),
        "ok": not missing,
    }


# ---------- 4. 仓库级凭据来源推定（derived_from）----------

def estimate_derived_from(session: Session) -> int:
    """历史 rc('bot') 行没有来源标记：与某平台行的 bot token 相同 → 回填 provider:username。

    D110：来源账号目录退役，来源改为平台行（一平台一个 bot）。
    """
    plain: dict[str, tuple[str, str]] = {}
    from sqlalchemy import select

    from src.kernel.persistence.models_platform_object import RepoPlatformModel

    for row in session.execute(select(RepoPlatformModel)).scalars().all():
        enc = getattr(row, "bot_token_enc", "") or ""
        if not enc:
            continue
        try:
            plain[_decrypt(enc)] = (row.provider, getattr(row, "bot_username", "") or "")
        except Exception as e:
            # 解密失败的行跳过（可能是旧密钥/脏值），迁移继续
            log.warning(f"platform migration: decrypt platform bot token failed provider={row.provider}: {e}")
            continue
    if not plain:
        return 0
    n = 0
    for cred_id, repo_key, value in session.execute(
        text("SELECT id, repo_key, value FROM grape_t_repo_credentials WHERE cred_type='bot'")
    ).fetchall():
        if not value:
            continue
        try:
            token = _decrypt(str(value))
        except Exception as e:
            log.warning(f"platform migration: decrypt repo bot credential failed id={cred_id}: {e}")
            continue
        hit = plain.get(token)
        if not hit:
            continue
        prov, username = hit
        session.execute(
            text("UPDATE grape_t_repo_credentials SET derived_from=:d WHERE id=:i"),
            {"d": f"{prov}:{username}".rstrip(":"), "i": cred_id},
        )
        n += 1
    return n


# ---------- 总入口 ----------

def run_all(session: Session) -> dict:
    stats = MigrationStats()
    report: dict = {"ok": False, "missing": [], "missing_count": -1}
    try:
        stats.oauth_channels = migrate_oauth_client_credentials(session)
        migrate_platform_bots(session, stats)
        back = backfill_user_platform_links(session)
        stats.pat_links = back.pat_links
        stats.oauth_links = back.oauth_links
        stats.skipped += back.skipped
        stats.derived_from = estimate_derived_from(session)
        report = reconcile_visible_platforms(session)
    except Exception as e:
        stats.errors.append(f"{type(e).__name__}: {e}")
        raise
    finally:
        log.info("D109 migration: %s | reconcile ok=%s missing=%s",
                 stats.as_dict(), report.get("ok"), report.get("missing_count"))
    return {"stats": stats.as_dict(), "reconcile": report}