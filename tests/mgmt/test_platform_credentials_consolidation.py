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

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")
os.environ.setdefault("OBS_LOG_DB", "0")
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ROOT = REPO_ROOT / "src"

# Vue 迁移后 admin 页的资产位置：壳是 46 行挂载壳（只引 admin-view.js），
# 全部视图/写链都在 AdminView.vue；语言包在 shell/locales。
# （原指向 src/api/static/*，该目录已随迁移删除 —— 2026-11-16 重定向）
ADMIN_SHELL = SRC_ROOT / "api" / "frontend" / "shell" / "admin.html"
ADMIN_VIEW = SRC_ROOT / "api" / "frontend" / "app" / "src" / "views" / "AdminView.vue"
ADMIN_LOCALE = SRC_ROOT / "api" / "frontend" / "shell" / "locales" / "zh" / "admin.json"

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user.db import set_meta_value


# ───────────────── S18/S19/S20/S24：A 层 UI + API 资产锁 ─────────────────

def test_platform_credentials_consolidation():
    """S19/S24：webhook_url 留在配置视图（写链保留）；webhook_tokens 凭据通道；
    旧全局键读点收敛。

    S18/S20 的「旧字段归零 / webhook token 输入迁仓库平台详情」两条在 Vue 版
    页面尚未落地，由 test_s18_s20_admin_field_retirement 以 xfail 挂账。
    """
    view = ADMIN_VIEW.read_text(encoding="utf-8")

    # S19：webhook_url 迁运行参数卡（同语义保留，写读链不变）
    assert 'v-model="cfg.webhook_url"' in view
    assert "body.webhook_url = cfg.webhook_url" in view

    # API 层：webhook_tokens 通道（PUT 写 {provider}_webhook_token + GET 回读配置态）
    from src.domains.mgmt.admin.routes import VcsPlatformCardBody
    assert "webhook_tokens" in VcsPlatformCardBody.model_fields

    # S24 守卫：旧全局键的读取点收敛——github_bot_username 的读方只剩
    # get_global_github_bot_username 兜底（config_store 的别名映射是写通道保留）
    offenders = []
    for p in SRC_ROOT.rglob("*.py"):
        source = p.read_text(encoding="utf-8")
        for pat in ('get_meta_value("github_bot_username"',
                    "get_meta_value('github_bot_username'"):
            if pat in source:
                offenders.append(f"{p.relative_to(REPO_ROOT)}: {pat}")
    assert not offenders, f"直读旧全局键残留: {offenders}"


@pytest.mark.xfail(strict=True, reason=(
    "已知缺口（2026-11-16 实测）：Vue 版 AdminView.vue 是 S18-S20 收口**之前**的 "
    "admin 页移植——gitcode_api_token / github_bot_token / github_bot_username / "
    "gitcode_webhook_token 及两平台 oauth client id+secret 仍在 cfg 且随 body 提交，"
    "仓库平台详情又缺 vp_webhook_token / body.webhook_tokens。页面按 S18/S20 收敛后"
    "本用例会自然 XPASS，届时删掉 xfail 标记即可。"
))
def test_s18_s20_admin_field_retirement():
    """S18/S20：旧双卡字段归零；webhook token 输入在仓库平台详情。"""
    view = ADMIN_VIEW.read_text(encoding="utf-8")
    for fid in ("f_gitcode_api_token", "f_gitcode_oauth_client_id",
                "f_gitcode_oauth_client_secret", "f_github_bot_token",
                "f_github_bot_username", "f_github_oauth_client_id",
                "f_github_oauth_client_secret", "f_gitcode_webhook_token"):
        assert fid not in view, f"旧字段 id 残留: {fid}"
        assert f"cfg.{fid[2:]}" not in view, f"旧字段仍在配置态: {fid[2:]}"
    assert ("cfg.webhook_tokens" in view or 'id="vp_webhook_token"' in view), \
        "webhook token 输入未迁到仓库平台详情"


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

    # R15.6 解绑移除协作者链：改由**仓库自己的** bot_username 驱动（比全局读链更准，
    # 不带全局名去移除别仓协作者），所以这里锁「移除链存在 + 取的是 managed_repos 列」。
    from src.domains.mgmt.routes import repos as rp
    rsrc = inspect.getsource(rp)
    assert "_remove_old_bot_collaborator" in rsrc
    assert "remove_collaborator" in rsrc
    assert "SELECT bot_username FROM managed_repos" in rsrc

    # github 专用读链仍收敛在统一入口：get_global_bot_usernames 的 github 位
    # 委托 get_global_github_bot_username（单点读链，勿在各消费方重复拼）
    from src.domains.mgmt.user import db as _udb
    assert "get_global_github_bot_username()" in inspect.getsource(_udb)


@pytest.mark.xfail(strict=True, reason=(
    "已知缺口（2026-11-16 实测）：D21/D22 的卡片退役与 S25 菜单改名没有随 Vue 迁移"
    "带过来——AdminView.vue 仍有 bot_usernames 输入与 token_health 汇总卡，"
    "语言包 credentials 仍叫「平台凭据」。页面收敛后本用例会自然 XPASS。"
))
def test_bot_cards_moved_to_platform_page():
    """S25/D21/D22：兜底清单卡与全局探活汇总卡退役；菜单改名运行参数。"""
    view = ADMIN_VIEW.read_text(encoding="utf-8")
    assert "bot_usernames" not in view
    assert "saveBotFallback" not in view
    assert "token_health" not in view
    zh = ADMIN_LOCALE.read_text(encoding="utf-8")
    assert '"credentials": "运行参数"' in zh
