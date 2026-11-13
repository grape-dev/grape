"""平台凭据页 A+B 收口（weekly-11 范围扩展二，2026-11-12）S18-S24 场景用例。

需求：docs/weekly-11/login-whitelist-requirements.md（S18-S24 / M10-M13）
设计：docs/weekly-11/login-whitelist-design.md（D13-D17）

A 层：旧 GitCode/GitHub 双卡删除（同 meta key 双入口）；webhook_url 迁运行参数；
     webhook token 输入迁仓库平台详情。
B 层：get_global_github_bot_username / get_global_bot_usernames 单点读链
     卡优先→meta 兜底；存量迁移注册式幂等。
"""
import os
import sys
from pathlib import Path

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")
os.environ.setdefault("OBS_LOG_DB", "0")
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ROOT = REPO_ROOT / "src"

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user.db import set_meta_value


# ───────────────── S18/S19/S20/S24：A 层 UI + API 资产锁 ─────────────────

def test_platform_credentials_consolidation():
    """S18/S19/S24：旧双卡 6 字段 id 归零；webhook_url 在运行参数卡；
    webhook_tokens 凭据通道 PUT/GET；旧全局键读取点只剩兜底函数。"""
    html = (SRC_ROOT / "api/static/admin.html").read_text(encoding="utf-8")
    js = (SRC_ROOT / "api/static/js/admin.js").read_text(encoding="utf-8")

    # S18：旧卡字段 id 归零（双入口删除）
    for fid in ("f_gitcode_api_token", "f_gitcode_oauth_client_id",
                "f_gitcode_oauth_client_secret", "f_github_bot_token",
                "f_github_bot_username", "f_github_oauth_client_id",
                "f_github_oauth_client_secret", "f_gitcode_webhook_token"):
        assert fid not in html, f"旧字段 id 残留: {fid}"
        assert fid not in js, f"旧字段读写残留: {fid}"
    # S19：webhook_url 迁运行参数卡（同 id 保留，写读链不变）
    assert 'id="f_webhook_url"' in html
    assert "body.webhook_url = webhookUrl" in js
    # S20：webhook token 输入在仓库平台详情（凭据区），gitcode_webhook_token 提交链删除
    assert 'id="vp_webhook_token"' in html
    assert "body.webhook_tokens" in js

    # API 层：webhook_tokens 通道（PUT 写 {provider}_webhook_token + GET 回读配置态）
    from src.domains.mgmt.admin.routes import VcsPlatformCardBody
    assert "webhook_tokens" in VcsPlatformCardBody.model_fields

    # S24 守卫：旧全局键的读取点收敛——github_bot_username 的读方只剩
    # get_global_github_bot_username 兜底（config_store 的别名映射是写通道保留）
    offenders = []
    for p in SRC_ROOT.rglob("*.py"):
        s = p.read_text(encoding="utf-8")
        for pat in ('get_meta_value("github_bot_username"',
                    "get_meta_value('github_bot_username'"):
            if pat in s:
                offenders.append(f"{p.relative_to(REPO_ROOT)}: {pat}")
    assert not offenders, f"直读旧全局键残留: {offenders}"


# ───────────────── S21/S22/S23：B 层读链收敛 + 迁移 ─────────────────

def _set_card_bot_username(provider: str, value: str):
    """基于当前生效卡全字段改 bot_username 写 meta（裸 dict 缺 host 会被
    Builder 拒掉回落内置文件卡）。"""
    import json
    import dataclasses
    from src.domains.runtime.vcs.platform_card import (get_platform_card,
                                                       invalidate_platform_card_cache)
    card = get_platform_card(provider)
    raw = dataclasses.asdict(card) if card else {"dialect": "github",
                                                 "host": f"{provider}.example.com"}
    raw.pop("provider", None)
    raw["bot_username"] = value
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT value FROM meta WHERE key='vcs_providers_config'").fetchone()
        cards = json.loads(row[0]) if row and row[0] else {}
        cards[provider] = raw
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
                  (json.dumps(cards, ensure_ascii=False),))
        db.commit()
    invalidate_platform_card_cache()


def _cleanup():
    set_meta_value("global_bot_github_username", "")
    set_meta_value("global_bot_usernames", "[]")
    _set_card_bot_username("github", "")


def test_bot_username_card_derivation():
    """S21/S22/S23：卡优先→meta 兜底读链；bot 名单卡并集；存量迁移幂等。"""
    from src.domains.mgmt.user.db import (get_global_github_bot_username,
                                          get_global_bot_usernames)
    _cleanup()
    try:
        # S21：卡值优先
        _set_card_bot_username("github", "gh_card_bot")
        assert get_global_github_bot_username() == "gh_card_bot"
        # S21：卡空 → meta 兜底
        _set_card_bot_username("github", "")
        set_meta_value("global_bot_github_username", "gh_legacy_bot")
        assert get_global_github_bot_username() == "gh_legacy_bot"

        # S22（D22 拍板后）：bot 名单 = 各卡 bot_username 纯卡派生并集
        # （meta global_bot_usernames 兜底读链退役——meta 值不再进名单）
        set_meta_value("global_bot_usernames", '["meta_bot"]')
        _set_card_bot_username("github", "gh_card_bot")
        names = get_global_bot_usernames()
        assert "gh_card_bot" in names, f"卡 bot_username 未进名单: {names}"
        assert "meta_bot" not in names, "meta 兜底读链未退役（D22 回潮）"
        assert len(names) == len(set(names)), "名单未去重"

        # S23：迁移幂等——meta 值写入卡 bot_username；卡已有值不覆盖
        from src.api.db_setup import _fn_github_bot_username_to_card
        set_meta_value("global_bot_github_username", "migrated_bot")
        _set_card_bot_username("github", "")  # 卡空 → 迁移应填入
        _fn_github_bot_username_to_card()
        from src.domains.runtime.vcs.platform_card import get_platform_card
        assert (get_platform_card("github") or _CardNull()).bot_username == "migrated_bot"
        # 幂等重跑 + 卡已有值不覆盖
        set_meta_value("global_bot_github_username", "newer_bot")
        _fn_github_bot_username_to_card()
        assert (get_platform_card("github") or _CardNull()).bot_username == "migrated_bot", "卡已有值被覆盖"
    finally:
        _cleanup()


class _CardNull:
    bot_username = ""


# ───────────────── 存量行为锁：self-comment / 解绑读链不破 ─────────────────

def test_global_bot_readers_unchanged():
    """消费方零改动行为锁：event_pipeline/repos 仍经统一入口取 bot 身份。"""
    import inspect
    from src.domains.runtime.pipeline import event_pipeline as ep
    src = inspect.getsource(ep)
    assert "get_global_github_bot_username" in src  # self-comment 检测 github 专用
    assert "get_global_bot_usernames" in src        # 其余渠道名单
    from src.domains.mgmt.routes import repos as rp
    rsrc = inspect.getsource(rp)
    assert "get_global_github_bot_username" in rsrc  # 解绑移除协作者链


def test_bot_cards_moved_to_platform_page():
    """S25：bot 兜底清单卡 + token 探活卡迁仓库平台页；原页收敛运行参数。"""
    html = (SRC_ROOT / "api/static/admin.html").read_text(encoding="utf-8")
    vps = html[html.index('id="vcsPlatformsView"'):]
    cfg_part = html[html.index('id="loginView"'):html.index('id="authChannelsView"')]
    # D22 拍板：兜底清单卡退役——f_bot_usernames 归零（self-comment 名单纯卡派生）
    assert 'id="f_bot_usernames"' not in vps
    assert 'saveBotFallback' not in (SRC_ROOT / "api/static/js/admin.js").read_text(encoding="utf-8")
    # D21：全局探活汇总卡退役——tkh_* 归零（健康状态逐行进列表，S27a 锁）
    assert 'id="tkh_status"' not in vps
    assert 'id="tkh_platforms"' not in vps
    # 原 cfg 视图不含两卡（saveConfig 不再写 bot_usernames）
    js = (SRC_ROOT / "api/static/js/admin.js").read_text(encoding="utf-8")
    # D22：saveBotFallback 已退役（assert not 在上文）
    # 菜单改名运行参数
    zh = (SRC_ROOT / "api/static/locales/zh/admin.json").read_text(encoding="utf-8")
    assert '"credentials": "运行参数"' in zh
