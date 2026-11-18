"""D109 导入 / 扫描服务（配置派生四表的唯一写入口）。

语义（d109 §8）：
- 判定依据 = **规范化内容 hash（canonical_hash），不用 mtime**；
- 逐 provider 六类判定：表空全量导入 / hash 变导入 / hash 同跳过 / 文件缺 → orphaned 软删 /
  manual 行不碰 / orphaned 行恢复（无视 hash）；
- 配置里出现凭据（client_id / client_secret）→ 剥离 + 告警，绝不写入 config_hash 与定义字段；
- link：显式 repo_links 的 repo_provider 必须存在于平台集合（否则该渠道 failed，响亮）；
  缺省派生仅在「自己就是仓库平台」时派生 identity_only（纯登录渠道不派生幽灵 link）；
- 写操作同一事务内 `epoch += 1`（meta.config_state），供各实例比对刷新；
- 导入锁：meta `config_import_lock` = `owner|expire_ts`，多实例并发启动只有一个执行。
"""
from __future__ import annotations

from src.kernel.config import log

import glob
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy.orm import Session

from src.kernel.config import CONFIG_DIR, log
from src.kernel.persistence.repositories_platform_object import (
    ChannelRepoLinkRepo,
    OAuthChannelRepo,
    RepoPlatformRepo,
)
from src.domains.platform.canonical import canonical_hash, strip_doc_keys

PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
RESERVED_PROVIDERS = frozenset({"local", "default", "meta", "system", "admin", "session", "anonymous"})

CHANNELS_FILE = "oauth-providers.json"
# 渠道↔平台授权（谁能驱动谁）= 独立配置文件，与表 grape_t_channel_repo_link 同名同构。
# 为什么不塞进 oauth-providers.json：那是登录协议（怎么登录），授权是仓库侧事实；
# 为什么不塞进 repo-credentials.json：那是凭据能力（能用哪种 token），授权不是凭据。
LINK_FILE = "channel-repo-links.json"
PLATFORMS_DIR = "platforms"
STATE_META_KEY = "config_state"
LOCK_META_KEY = "config_import_lock"

# 配置文件里出现则剥离并告警（凭据不进配置；D108 纪律）
CREDENTIAL_KEYS = ("client_id", "client_secret")


@dataclass
class Change:
    provider: str
    action: str  # inserted | updated | skipped | manual_kept | restored
    reason: str = ""


@dataclass
class ScanReport:
    channels: list[Change] = field(default_factory=list)
    platforms: list[Change] = field(default_factory=list)
    links: dict[str, int] = field(default_factory=dict)
    orphaned: dict[str, list[str]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    epoch_before: int = 0
    epoch_after: int = 0
    applied: bool = False

    @property
    def changed(self) -> int:
        acts = {"inserted", "updated", "restored"}
        return sum(1 for c in self.channels + self.platforms if c.action in acts)

    def summary(self) -> str:
        return (
            f"providers: channel={len(self.channels)} platform={len(self.platforms)} "
            f"changed={self.changed} "
            f"orphaned={sum(len(v) for v in self.orphaned.values())} failed={len(self.failed)} "
            f"epoch {self.epoch_before}->{self.epoch_after}"
        )


# ---------- meta 状态（epoch / 锁 / 就绪） ----------

def _read_state() -> dict:
    from src.domains.mgmt.public import get_meta_value  # 跨域走 owner public

    raw = get_meta_value(STATE_META_KEY) or ""
    if not raw:
        return {"epoch": 0, "migrated": False}
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    out = dict(data) if isinstance(data, dict) else {}
    out["epoch"] = int(out.get("epoch") or 0)
    out["migrated"] = bool(out.get("migrated"))
    return out  # 保留其它标记（credentials_migrated 等），不得只写两项


def _write_state(state: dict) -> None:
    from src.domains.mgmt.public import set_meta_value  # 跨域走 owner public

    payload = dict(state)
    payload["epoch"] = int(payload.get("epoch") or 0)
    payload["migrated"] = bool(payload.get("migrated"))
    set_meta_value(STATE_META_KEY, json.dumps(payload))


def current_epoch() -> int:
    return _read_state()["epoch"]


def bump_epoch() -> int:
    """表写入后调用（同事务语义由调用方保证；meta 写入内部 commit）。"""
    st = _read_state()
    st["epoch"] = int(st.get("epoch") or 0) + 1
    _write_state(st)
    return st["epoch"]


def is_table_backed() -> bool:
    """读路径切换判据：导入已完成（migrated）后才允许纯表读取。

    未就绪时调用方必须回落旧存储（文件 + meta），这是 §16 影响 #1 的缓解。
    """
    from src.kernel.persistence.engine import get_engine
    from sqlalchemy import select
    from src.kernel.persistence.models_platform_object import OAuthChannelModel

    st = _read_state()
    if st.get("migrated"):
        return True
    try:
        from sqlalchemy.orm import Session

        with Session(get_engine()) as s:
            return s.execute(select(OAuthChannelModel.id).limit(1)).first() is not None
    except Exception:
        return False


def mark_migrated() -> None:
    st = _read_state()
    st["migrated"] = True
    _write_state(st)


def acquire_import_lock(owner: str, ttl_s: int = 120) -> bool:
    """meta 层租约锁：`owner|expire_ts`。过期可抢占（防实例崩溃后永久锁）。"""
    from src.domains.mgmt.public import get_meta_value  # 跨域走 owner public

    now = time.time()
    raw = get_meta_value(LOCK_META_KEY) or ""
    if raw:
        try:
            _o, _exp = raw.split("|", 1)
            if float(_exp) > now:
                return False
        except Exception as e:
            # 锁值形如 "<owner>|<exp>"；脏值按「过期」处理，允许重新获取
            log.debug(f"platform importer: unparsable lock value {raw!r}: {e}")
    # 先写再读回校验（并发下两个实例同时写，后写者赢；再读一次确认自己是持有者）
    from src.domains.mgmt.public import set_meta_value  # 跨域走 owner public

    set_meta_value(LOCK_META_KEY, f"{owner}|{now + ttl_s}")
    got = get_meta_value(LOCK_META_KEY) or ""
    return got.split("|", 1)[0] == owner


def release_import_lock(owner: str) -> None:
    from src.domains.mgmt.public import get_meta_value, set_meta_value  # 跨域走 owner public

    raw = get_meta_value(LOCK_META_KEY) or ""
    if raw.split("|", 1)[0] == owner:
        set_meta_value(LOCK_META_KEY, "")


# ---------- 配置加载 ----------

def _read_json(path: str) -> tuple[dict | None, str]:
    if not os.path.exists(path):
        return None, ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None, f"{os.path.basename(path)}: 顶层必须是对象"
        return data, ""
    except Exception as e:  # 单文件坏只影响该文件
        return None, f"{os.path.basename(path)}: JSON 解析失败: {e}"


def _valid_provider(provider: str) -> str:
    if not PROVIDER_RE.match(provider or ""):
        return f"provider 命名非法: {provider!r}（要求 ^[a-z][a-z0-9_-]{{1,31}}$）"
    if provider in RESERVED_PROVIDERS:
        return f"provider 使用保留字: {provider!r}"
    return ""


def _strip_credentials(raw: dict) -> tuple[dict, dict]:
    """文件里的凭据剥离（告警），返回 (定义字段, 凭据)。凭据不参与 hash、不进配置表定义列。"""
    fields = dict(raw)
    creds: dict[str, str] = {}
    for key in CREDENTIAL_KEYS:
        if key in fields:
            val = fields.pop(key)
            if isinstance(val, str) and val.strip():
                creds[key] = val
    return fields, creds


def load_oauth_channels(config_dir: str | None = None) -> tuple[dict[str, dict], list[str]]:
    base = config_dir or CONFIG_DIR
    data, err = _read_json(os.path.join(base, CHANNELS_FILE))
    out: dict[str, dict] = {}
    errs: list[str] = [err] if err else []
    for provider, raw in (data or {}).items():
        if provider.startswith("_"):
            continue  # 文档键（_readme/_fields/_note…）：说明文本，不是 provider
        if not isinstance(raw, dict):
            errs.append(f"channel:{provider}: 值必须是对象")
            continue
        bad = _valid_provider(provider)
        if bad:
            errs.append(f"channel:{provider}: {bad}")
            continue
        fields, creds = _strip_credentials(raw)
        if creds:
            errs.append(f"channel:{provider}: 配置文件含凭据键 {sorted(creds)}（已剥离，请用管理面/迁移写入表加密列）")
        out[provider] = strip_doc_keys(fields)
    return out, errs


def load_repo_platforms(config_dir: str | None = None) -> tuple[dict[str, dict], list[str]]:
    base = config_dir or CONFIG_DIR
    out: dict[str, dict] = {}
    errs: list[str] = []
    for path in sorted(glob.glob(os.path.join(base, PLATFORMS_DIR, "*.json"))):
        provider = os.path.splitext(os.path.basename(path))[0]
        bad = _valid_provider(provider)
        if bad:
            errs.append(f"platform:{provider}: {bad}")
            continue
        data, err = _read_json(path)
        if err:
            errs.append(f"platform:{provider}: {err}")
            continue
        if data is not None:
            out[provider] = strip_doc_keys(data)
    return out, errs


# ---------- 扫描 / 应用 ----------

def _default_links(channels: dict, platforms: dict) -> dict[str, list[dict]]:
    """没有授权配置文件时的初始化默认：**空**（同域天然可用，不需要派生同域行）。

    2026-11-14 定稿（用户裁定"同域配对是错误配置"）：渠道自己就是仓库平台时无需配对，
    运行时 `hooks._targets` 会隐式补上自己。文件不存在 = 只跨名需要显式声明；
    文件存在即以其为准（links 为空 = 没有任何跨名授权，同域仍可用）。
    """
    return {}


def load_channel_links(config_dir: str | None = None) -> tuple[dict[str, list[dict]], list[str]]:
    """渠道↔平台授权：显式声明，按 oauth_provider 分组。缺省=不授权（不再隐式派生）。"""
    base = config_dir or CONFIG_DIR
    data, err = _read_json(os.path.join(base, LINK_FILE))
    errs: list[str] = [err] if err else []
    out: dict[str, list[dict]] = {}
    rows = (data or {}).get("links")
    if rows is None:
        return out, errs
    if not isinstance(rows, list):
        return out, errs + [f"link: links 必须是数组"]
    seen: set[tuple[str, str]] = set()
    for item in rows:
        if not isinstance(item, dict):
            errs.append("link: 条目必须是对象")
            continue
        oauth = str(item.get("oauth_provider") or "")
        repo = str(item.get("repo_provider") or "")
        if not oauth or not repo:
            errs.append("link: 缺 oauth_provider/repo_provider")
            continue
        if (oauth, repo) in seen:
            errs.append(f"link:{oauth}->{repo}: 重复声明")
            continue
        seen.add((oauth, repo))
        if oauth == repo:
            # 同域不是授权：渠道自己即平台，天然可驱动；配进来只会生成无意义数据
            errs.append(f"link:{oauth}->{repo}: 同域配对无意义（渠道自己就是该平台），已忽略")
            continue
        # 只记"这对（渠道 → 平台）可用"；relation 档位已取消（2026-11-14 用户裁定）
        out.setdefault(oauth, []).append({"repo_provider": repo, "source": "config"})
    return out, errs


def scan_and_apply(
    session: Session,
    *,
    config_dir: str | None = None,
    apply: bool = False,
    bump_epoch_after_commit: bool = True,
) -> ScanReport:
    """扫描三类配置并与表对账；apply=True 时在外部 UoW 内写表（本函数不 commit）。

    调用方负责事务边界（uow()），保证「四表写入 + epoch 递增」同一事务。
    """
    report = ScanReport(epoch_before=current_epoch())
    channels, e1 = load_oauth_channels(config_dir)
    platforms, e2 = load_repo_platforms(config_dir)
    report.warnings.extend(e1 + e2)

    # 历史双面字段：渠道卡里的 pat_create_url 属于平台卡（2026-11-14 前经 caps 归位），丢弃
    for fields in channels.values():
        fields.pop("pat_create_url", None)

    # 平台卡里的 `owner_identity_token_fallback` 是历史键名：落到平台行同名语义列
    for fields in platforms.values():
        if "owner_identity_token_fallback" in fields:
            fields["owner_fallback"] = fields.pop("owner_identity_token_fallback")

    platform_set = set(platforms)

    # 未知键校验：apply 与 preview 必须一致（否则 preview 说 OK、apply 失败）
    from src.kernel.persistence.repositories_platform_object import _PROTECTED, _column_names

    def _validate(tag: str, model, mapping: dict[str, dict]) -> dict[str, dict]:
        kept: dict[str, dict] = {}
        cols = _column_names(model)
        # 领域字段 → 列名别名（upsert_from_config 内会转换）
        # 无领域字段 → 列名别名（drivers 概念已删除；repo_links 归 channel-repo-links.json）
        alias: dict[str, str] = {}
        for provider, fields in mapping.items():
            bad = sorted(
                k for k in fields
                if k not in cols and k not in _PROTECTED and alias.get(k) not in cols
            )
            if bad:
                report.failed.append(f"{tag}:{provider}: 未知配置键 {bad}（整条拒绝）")
                continue
            kept[provider] = fields
        return kept

    from src.kernel.persistence.models_platform_object import (
        OAuthChannelModel as _ChM, RepoPlatformModel as _PlM,
    )

    channels = _validate("channel", _ChM, channels)
    platforms = _validate("platform", _PlM, platforms)
    platform_set = set(platforms)

    # 渠道↔平台授权：独立配置文件优先；文件不存在 → 初始化默认按 provider 同名关联
    links_by_channel, link_errs = load_channel_links(config_dir)
    report.failed.extend(link_errs)
    _link_path = os.path.join(config_dir or CONFIG_DIR, LINK_FILE)
    if not os.path.exists(_link_path):
        links_by_channel = _default_links(channels, platforms)
        report.warnings.append(
            f"link: 未提供 {LINK_FILE}：同域渠道天然可驱动本平台，跨名授权需显式声明（当前无跨名配对）")

    def _one(tag: str, bucket: list, fn, provider: str, fields: dict) -> None:
        """单 provider 失败只记 failed，不拖垮整批（未知键/非法值整条拒绝）。"""
        try:
            # SAVEPOINT：单条失败只回滚该条，不牵连整批（不能用 session.rollback，那会丢已成功的写入）
            with session.begin_nested():
                bucket.append(Change(provider, fn(session, provider, fields, canonical_hash(fields))))
        except Exception as e:
            report.failed.append(f"{tag}:{provider}: {type(e).__name__}: {e}")

    if apply:
        for provider, fields in channels.items():
            _one("channel", report.channels, OAuthChannelRepo.upsert_from_config, provider, fields)
        for provider, fields in platforms.items():
            _one("platform", report.platforms, RepoPlatformRepo.upsert_from_config, provider, fields)
        # link：合并导入（manual 优先）；数据来自 channel-repo-links.json，
        # 目标平台/渠道缺失 = 配置错误（整渠道 failed，保留旧行不动）
        for provider in channels:
            links = []
            for item in links_by_channel.get(provider, []):
                if item["repo_provider"] not in platform_set:
                    report.failed.append(
                        f"link:{provider}->{item['repo_provider']}: 目标仓库平台不在 platforms/ 集合（整渠道拒绝）")
                    links = None
                    break
                links.append(item)
            if links is None:
                continue
            report.links[provider] = ChannelRepoLinkRepo.merge_for_oauth(
                session, provider, links, canonical_hash(links))
        # 指向了未声明渠道的授权行：响亮记录（不写库）
        for oauth in links_by_channel:
            if oauth not in channels:
                report.failed.append(f"link:{oauth}: oauth_provider 不在 oauth-providers.json（忽略该渠道的授权行）")

        report.orphaned["channels"] = OAuthChannelRepo.mark_orphaned(session, channels)
        report.orphaned["platforms"] = RepoPlatformRepo.mark_orphaned(session, platforms)
        report.applied = True
    else:
        # preview：只给动作预估，不写库
        for provider, fields in channels.items():
            row = OAuthChannelRepo.get(session, provider)
            action = "inserted" if row is None else (
                "manual_kept" if row.source == "manual" else
                "restored" if row.source == "orphaned" else
                "skipped" if (row.config_hash or "") == canonical_hash(fields) else "updated")
            report.channels.append(Change(provider, action))
        for provider, fields in platforms.items():
            row = RepoPlatformRepo.get(session, provider)
            action = "inserted" if row is None else (
                "manual_kept" if row.source == "manual" else
                "restored" if row.source == "orphaned" else
                "skipped" if (row.config_hash or "") == canonical_hash(fields) else "updated")
            report.platforms.append(Change(provider, action))
        for provider in channels:
            for item in links_by_channel.get(provider, []):
                if item["repo_provider"] not in platform_set:
                    report.failed.append(
                        f"link:{provider}->{item['repo_provider']}: 目标仓库平台不在 platforms/ 集合（整渠道拒绝）")
                    break
        report.orphaned["channels"] = [p for p in (r.provider for r in OAuthChannelRepo.list_all(session))
                                       if p not in channels]
        report.orphaned["platforms"] = [p for p in (r.provider for r in RepoPlatformRepo.list_all(session))
                                        if p not in platforms]

    _dirty = bool(report.changed) or any(bool(v) for v in report.orphaned.values())
    report.epoch_after = (bump_epoch() if (apply and _dirty and bump_epoch_after_commit)
                          else report.epoch_before)
    return report


def run_startup_import(session: Session, *, owner: str, config_dir: str | None = None) -> ScanReport:
    """启动/显式导入入口：抢锁 → 扫描应用 → 标记 migrated → 放锁。

    读路径未就绪时的回落由调用方（providers.py / platform_card.py）依据 is_table_backed() 决定。
    """
    got = acquire_import_lock(owner)
    try:
        if not got:
            log.info("platform-object import: 另一实例持有导入锁，跳过（等 epoch 刷新）")
            return ScanReport(epoch_before=current_epoch(), epoch_after=current_epoch())
        report = scan_and_apply(session, config_dir=config_dir, apply=True,
                                bump_epoch_after_commit=False)
        session.commit()  # 先落表，再用独立连接写 epoch/migrated（避免 SQLite 写锁互斥）
        if report.changed or any(report.orphaned.values()):
            report.epoch_after = bump_epoch()
        mark_migrated()
        log.info("platform-object import: %s", report.summary())
        for w in report.warnings:
            log.warning("platform-object import warning: %s", w)
        for f in report.failed:
            log.error("platform-object import failed: %s", f)
        return report
    finally:
        if got:
            release_import_lock(owner)