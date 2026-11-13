"""管理面卡管理（weekly-11 范围扩展，2026-11-12）S12-S16 场景用例。

需求：docs/weekly-11/login-whitelist-requirements.md（S12-S16）
设计：docs/weekly-11/login-whitelist-design.md（D9-D12）

覆盖：
- S12/S13 登录渠道列表（全卡字段+client_id 明文+secret 掩码）与合并式编辑
- S14 仓库平台列表全字段回读（编辑回写零丢失，S70 反例锁）
- S15 bot/botname 编辑（bot_username 走卡；bot token 走 global_bot_meta_key）
- S16 管理面新菜单 UI 资产锁（卡驱动动态渲染，零平台字面量）
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

from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user.db import get_meta_value, set_meta_value


def _admin_client():
    from src.api.main import app
    c = TestClient(app)
    r = c.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return c


def _cleanup():
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key IN "
                            "('vcs_providers_config', 'global_bot_testplat_token')")
        db.commit()
    from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
    invalidate_platform_card_cache()


# ───────────────── S12/S13 登录渠道列表+编辑 ─────────────────

def test_admin_auth_channels_list_full_card():
    """S12：GET /admin/auth-channels 返回全卡字段 + client_id 明文 + secret 掩码。"""
    c = _admin_client()
    r = c.get("/api/admin/auth-channels")
    assert r.status_code == 200, r.text
    providers = r.json()["providers"]
    assert providers, "渠道目录为空"
    for p in providers:
        assert "card" in p and isinstance(p["card"], dict), f"{p['provider']} 缺全卡字段"
        assert "client_id" in p
        assert "client_secret_set" in p
        assert "client_secret" not in p["card"], "secret 值不得回读"
    # 内置默认渠道在册
    names = {p["provider"] for p in providers}
    assert {"gitcode", "github", "gitee"} <= names


def test_admin_auth_channels_edit_merge():
    """S13：编辑保存合并式（不挤掉其他渠道）+ secret 留空不改 + 热失效。"""
    c = _admin_client()
    set_meta_value("oauth_gitee_client_secret", "preset-secret")
    try:
        # 只提交 gitcode 一张卡
        body = {"providers": {"gitcode": {
            "name": "GitCode", "auth_url": "https://gitcode.com/oauth/authorize",
            "token_url": "https://gitcode.com/oauth/token",
            "user_url": "https://gitcode.com/api/v5/user",
            "scope": "all", "id_field": "id", "login_field": "login",
            "pkce_enabled": True,
        }}}
        r = c.put("/api/admin/auth-channels", json=body)
        assert r.status_code == 200, r.text
        # 合并式：gitee 仍在目录
        r2 = c.get("/api/admin/auth-channels")
        names = {p["provider"] for p in r2.json()["providers"]}
        assert "gitee" in names, "合并式保存挤掉了未提交渠道"
        # secret 留空=不改
        assert get_meta_value("oauth_gitee_client_secret") == "preset-secret"
    finally:
        set_meta_value("oauth_gitee_client_secret", "")


# ───────────────── S14 仓库平台全字段回读 ─────────────────

def test_admin_vcs_platforms_list_full_fields():
    """S14：GET /admin/vcs-platforms 全字段回读（含平台私有位），编辑回环零丢失。"""
    from src.domains.runtime.vcs.platform_card import build_platform_card
    c = _admin_client()
    r = c.get("/api/admin/vcs-platforms")
    assert r.status_code == 200, r.text
    cards = r.json()["cards"]
    for provider, card in cards.items():
        # GET→PUT 回环合法：回读 dict 可直接喂 Builder
        rebuilt = build_platform_card(provider, card)
        assert rebuilt.host == card["host"]
        # 平台私有位在场（S70 反例锁：子集回读=编辑静默降级）
        for f in ("bot_username", "issue_write_form", "branch_create_form",
                  "token_storage_field", "global_bot_meta_key", "brand_color",
                  "legacy_token_symbol", "pat_create_url"):
            assert f in card, f"{provider} 全字段回读缺 {f}"
        assert "bot_token_configured" in card


# ───────────────── S15 bot/botname 编辑 ─────────────────

def test_admin_vcs_platforms_bot_token_channel():
    """S15：bot_username 走卡字段；bot_tokens 写 global_bot_meta_key；未知平台拒。"""
    _cleanup()
    c = _admin_client()
    try:
        card = {"dialect": "github", "host": "testplat.example.com",
                "api_auth": "query_token", "bot_username": "test_bot"}
        r = c.put("/api/admin/vcs-platforms", json={
            "cards": {"testplat": card},
            "bot_tokens": {"testplat": "tok-123"},
        })
        assert r.status_code == 200, r.text
        # bot token 落派生 meta key（卡未显式 global_bot_meta_key）
        assert get_meta_value("global_bot_testplat_token") == "tok-123"
        # bot_username 进卡并热生效
        from src.domains.runtime.vcs.platform_card import get_platform_card
        _c = get_platform_card("testplat")
        assert _c is not None and _c.bot_username == "test_bot"
        # GET 回读 bot_token_configured=True
        r2 = c.get("/api/admin/vcs-platforms")
        assert r2.json()["cards"]["testplat"]["bot_token_configured"] is True
        # 未知平台 bot_tokens 拒
        r3 = c.put("/api/admin/vcs-platforms", json={
            "cards": {"testplat": card},
            "bot_tokens": {"nosuch": "x"},
        })
        assert r3.status_code == 400
    finally:
        _cleanup()


# ───────────────── S16 管理面 UI 资产锁 ─────────────────

def test_admin_card_mgmt_ui_assets():
    """S16：侧边栏双菜单在位；详情表单卡驱动动态渲染（零平台字面量）；?v= bump。"""
    html = (SRC_ROOT / "api/static/admin.html").read_text(encoding="utf-8")
    js = (SRC_ROOT / "api/static/js/admin.js").read_text(encoding="utf-8")
    # 菜单与视图容器
    assert "showAuthChannels" in html and "authChannelsView" in html
    assert "showVcsPlatforms" in html and "vcsPlatformsView" in html
    assert 'data-i18n="admin.nav.auth_channels"' in html
    assert 'data-i18n="admin.nav.vcs_platforms"' in html
    # 视图互斥：既有切换函数已接入 _hideCardMgmtViews
    assert js.count("_hideCardMgmtViews()") >= 4, "showCfg/showUsers/showWhitelist/switch 未全部互斥"
    # 卡驱动动态渲染（字段来自 GET 响应，零 per-field 代码）
    assert "_renderCardFields" in js and "_collectCardFields" in js
    # 列表/详情/保存链路函数在位并全局暴露（HTML 内联 onclick 引用面）
    for fn in ("showAuthChannels", "viewAuthChannel", "backAuthChannels",
               "saveAuthChannel", "showVcsPlatforms", "viewVcsPlatform",
               "backVcsPlatforms", "saveVcsPlatform"):
        assert f"window.{fn} = {fn}" in js, f"{fn} 未全局暴露"
    # load* 为内部链路（由 show* 调起），无 onclick 引用面
    for fn in ("loadAuthChannels", "loadVcsPlatforms"):
        assert f"function {fn}" in js
    # 凭据字段表单元素在位
    assert 'id="ac_client_id"' in html and 'id="ac_client_secret"' in html
    assert 'id="vp_bot_token"' in html
    # 版本 bump（只升不降：覆盖批次1的 20261112-1）
    # 版本 bump 只升不降（S43 批次改日期段格式 YYYYMMDD-N）：日期段或同日序号
    # 任一前进即过——后续批次再 bump 无需改测试
    import re as _re
    _m = _re.search(r"admin\.js\?v=(\d{8})-(\d+)", html)
    assert _m, "admin.js 版本号缺失/格式漂移"
    _date, _seq = int(_m.group(1)), int(_m.group(2))
    assert (_date, _seq) >= (20261112, 6), "admin.js 版本号回退/未 bump"


def test_beta_apply_multi_channel_dropdown():
    """S17：申请注册页多渠道下拉——beta-apply.html 平台选项由渠道目录动态渲染，
    零字面量 option（gitee/华为被白名单拦下进来时 platform 参数可正确选中）。"""
    html = (SRC_ROOT / "api/static/beta-apply.html").read_text(encoding="utf-8")
    # 字面量双平台 option 退役
    assert '<option value="gitcode">GitCode</option>' not in html
    assert '<option value="github">GitHub</option>' not in html
    # 渠道目录动态渲染 + URL platform 参数就绪后设置
    assert "_loadChannelOptions" in html
    assert "fetch('/api/auth/providers')" in html


def test_admin_detail_tabs_and_per_platform_health():
    """S27a/S27b：探活状态逐平台并入 GET；详情页 iOS 分段 Tab 分组渲染。"""
    html = (SRC_ROOT / "api/static/admin.html").read_text(encoding="utf-8")
    js = (SRC_ROOT / "api/static/js/admin.js").read_text(encoding="utf-8")
    # S27b：分段控件 + 分组容器 + 切换函数
    for prefix in ("vp", "ac"):
        assert f'id="{prefix}Tabs"' in html
        for n in (1, 2, 3, 4):
            assert f'id="{prefix}CardF{n}"' in html
    assert "_switchCardTab" in js and "window._switchCardTab" in js
    assert "_CARD_TAB_FIELDS" in js  # 字段名清单分组（卡驱动，未匹配进"其他"）
    assert "CardF1 [data-cardfield]" in js  # collect 扫全 Tab 容器
    # S27a：探活并入每卡（routes 源码断言）
    import inspect
    from src.domains.mgmt.admin import routes as ar
    src = inspect.getsource(ar.vcs_platforms_list)
    assert "bot_token_health" in src and "checked_at" in src
    # 全局探活卡退役：tkh_* 元素归零；列表健康列在位
    assert "tkh_status" not in html and "tkh_platforms" not in html
    assert "col_health" in html
    assert "c.bot_token_health" in js  # 行内健康渲染
