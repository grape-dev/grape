"""前端模块缓存版本 ratchet（PR #1231 第 3 轮 Critical-2 / #1363 复审 R-1）。

回归背景：内容有实质变更的模块 ?v= 未 bump，浏览器会拿旧缓存跑新后端。人工纪律不可靠，
本守卫把**四个页面**里全部模块的 ?v= 冻结成精确映射——任何版本号变化（含新增模块）
都必须同步改这里，改守卫即强制过 review。精确等值天然满足「只升不降」。

边界说明：
- 日期型版本的语言包要 `i18n.js`（`?v=20261113-1`）不在本守卫内，由
  `tests/api/test_i18n_locale_cache_ratchet.py` 用「历史下界 + 内容指纹」守护；
- 第三方 vendor 库版本随打包固定，走白名单。
"""
import hashlib
import re
from pathlib import Path

SHELL = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "shell"
APP = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "app" / "src"

# 页面 → {模块: 冻结版本}（2026-11-13 缓存版本回填/统一后快照）
FROZEN = {
    'index.html': {
        'account-view.js': '67',
        'agent-view.js': '51',
        'all-views.css': '84',
        'api.js': '39',
        'app.css': '11',
        'app.js': '311',
        'auth.css': '1',
        'auto-recovery.js': '2',
        'autopilot-view.js': '74',
        'chat-view.js': '63',
        'dashboard-view.js': '92',
        'deeplink.js': '1',
        'event-view.js': '66',
        'faro.js': '8',
        'feedback-dialog.js': '50',
        'host-status.js': '1',
        'im-channel.js': '57',
        'issues-view.js': '74',
        'knowledge-view.js': '49',
        'login-view.js': '2',
        'my-feedback-detail.js': '50',
        'my-feedback.js': '50',
        'oauth-buttons.js': '2',
        'observability-view.js': '61',
        'onboarding-view.js': '11',
        'organizations.js': '85',
        'pipeline.js': '58',
        'platform-icons.js': '2',
        'plugins.js': '57',
        'pwd-blocklist.js': '3',
        'repos-view.js': '68',
        'runtimes-view.js': '95',
        'sandbox-view.js': '15',
        'scene-market.js': '62',
        'skills-view.js': '64',
        'tokens.css': '2',
        'worker-name.js': '2',
    },
    'admin.html': {
        'admin-view.css': '2',
        'admin-view.js': '51',
        'pwd-blocklist.js': '3',
        'tokens.css': '2',
    },
    'register.html': {
        'api.js': '39',
        'app.css': '10',
        'auth.css': '1',
        'auth.js': '8',
        'pwd-blocklist.js': '3',
        'register-view.js': '1',
    },
    'forgot.html': {
        'api.js': '39',
        'app.css': '10',
        'auth.css': '1',
        'auth.js': '8',
        'forgot-view.js': '3',
        'pwd-blocklist.js': '3',
    },
}
# 日期型版本模块：交给 i18n_locale_cache_ratchet 的「下界 + 指纹」守护
DATE_VERSIONED = {
    "i18n.js",
}

# 第三方库：版本号随 vendor 打包固定，不参与冻结校验
VENDOR = {
    "echarts.min.js",
    "faro-web-sdk.iife.js",
    "faro-web-tracing.iife.js",
    "jquery.min.js",
    "marked.min.js",
    "qrcode.min.js",
    "select2.min.js",
    "vue.global.prod.min.js",
}

REF = re.compile(r"([A-Za-z_.\-/]+\.(?:js|css))\?v=(\d+)(?=[\"'])")


def _scan(page: str) -> dict:
    html = (SHELL / page).read_text(encoding="utf-8")
    found = {}
    for mod, ver in REF.findall(html):
        name = mod.rsplit("/", 1)[-1]
        found.setdefault(name, set()).add(ver)
    # 同一页面重复引用同一模块却给了两个版本 → 至少一个是错的
    assert all(len(v) == 1 for v in found.values()), (
        f"{page} 内同一模块出现多个版本: "
        + str({k: sorted(v) for k, v in found.items() if len(v) > 1})
    )
    return {k: next(iter(v)) for k, v in found.items()}


def test_page_module_cache_versions_match_frozen_map():
    problems = []
    for page, frozen in FROZEN.items():
        found = _scan(page)
        problems += [f"{page}:{m} 冻结={v} 实际={found.get(m)}"
                     for m, v in frozen.items() if found.get(m) != v]
        unregistered = sorted(
            m for m in found
            if m not in frozen and m not in VENDOR and m not in DATE_VERSIONED
            and not m.startswith(".")
        )
        if unregistered:
            problems.append(f"{page} 新模块未登记进 FROZEN: {unregistered}")
    assert not problems, (
        "shell 页面模块 ?v= 与冻结映射不一致: " + "; ".join(problems)
        + "（版本只升不降；改 JS/CSS 必须 bump 并同步更新 FROZEN）"
    )


# 2026-11-14 两张卡片各自迁成 Vue view：两个页面各有自己的 chunk，
# 同构性只对「共享模块」成立（vue chunk 按页面唯一，不进分叉比对）。
PAGE_OWN_CHUNKS = {"register.html": {"register-view.js"}, "forgot.html": {"forgot-view.js"}}


def test_register_and_forgot_agree():
    """两封邮件页是同构复制品，共享模块版本必须一致（复审 N-1 的跨页分叉）。"""
    reg, fgt = _scan("register.html"), _scan("forgot.html")
    own = PAGE_OWN_CHUNKS["register.html"] | PAGE_OWN_CHUNKS["forgot.html"]
    diff = {m: (reg.get(m), fgt.get(m)) for m in set(reg) | set(fgt)
            if reg.get(m) != fgt.get(m) and m not in VENDOR and m not in own}
    assert not diff, f"register/forgot 版本分叉: {diff}"
    # 各自的专属 chunk 必须真实注册（走 Vue 后页面不再是同构复制品）
    for page, chunks in PAGE_OWN_CHUNKS.items():
        other = "forgot.html" if page == "register.html" else "register.html"
        for chunk in chunks:
            assert chunk in _scan(page), f"{page} 缺少专属 view chunk {chunk}"
            assert chunk not in _scan(other), f"{chunk} 不应出现在 {other} 上"


# —— all-views.css「内容指纹 + 版本」绑定——
# `all-views.css` 是 vite 构建产物（不入库）：= bridge.css + ag-ui.css（AG 组件样式聚合）
# + 各 view 的 scoped CSS 合并，`admin-view.css` 除外（AdminView.vue 的样式非 scoped、
# 只进 admin.html，见 vite.config.js 的 viewCssMap 与合并逻辑）。此前它只有版本号冻结、
# 无内容绑定——改了 scoped 样式却不动 `all-views.css?v=40` 时版本棘轮全绿，持缓存的
# 客户端继续吃旧样式。本守卫直接指纹其**源**（仿 i18n 棘轮）：
#   · 源集 = styles/bridge.css + 全部带 <style> 的 .vue（pages/ 未参与构建、admin 三件
#     的 CSS 单独走 admin-view.css，均排除）；任一源文件内容变化 → 红。
# 纪律：改上述任一源 → bump index.html 的 `all-views.css?v=`，并同步更新本处
# （版本, 指纹）对与上方 FROZEN（同一次提交）。

# all-views.css 与其源的（页面版本, 内容指纹）配对登记
ALL_VIEWS = ('84', 'cb9582d7619cf66d')

# admin 三件：AdminView.vue（非 scoped，样式走 admin-view.css）+ 只被 admin chunk
# 引用的 AdminChannels/AdminPlatforms（其 CSS 并入 admin-view.css，不进 all-views）
ALL_VIEWS_EXCLUDED = {"AdminView.vue", "AdminChannels.vue", "AdminPlatforms.vue"}


def _all_views_sources() -> list:
    srcs = [APP / "styles" / "bridge.css"]
    srcs += sorted(
        p for p in APP.rglob("*.vue")
        if "<style" in p.read_text(encoding="utf-8")
        and p.relative_to(APP).parts[0] != "pages"
        and p.name not in ALL_VIEWS_EXCLUDED
    )
    return srcs


def _all_views_fingerprint() -> str:
    h = hashlib.sha1()
    for s in _all_views_sources():
        h.update(str(s.relative_to(APP)).encode())
        h.update(b"\0")
        h.update(s.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def test_all_views_css_content_bound_to_version():
    """all-views.css 的源内容变了就必须抬版本并同步本文件的配对登记。"""
    href = re.search(r"all-views\.css\?v=(\d+)", (SHELL / "index.html").read_text(encoding="utf-8"))
    assert href, "index.html 未以 ?v= 引用 all-views.css"
    actual_ver, actual_fp = href.group(1), _all_views_fingerprint()
    reg_ver, reg_fp = ALL_VIEWS
    assert (actual_ver, actual_fp) == (reg_ver, reg_fp), (
        f"all-views.css 源内容/版本与登记不符：页面 ?v={actual_ver} 指纹={actual_fp}，"
        f"登记 ?v={reg_ver} 指纹={reg_fp}——改了 bridge.css / AG 组件 / 任一 view 的 "
        f"scoped 样式就必须 bump index.html 的 all-views.css?v=，"
        f"并同步更新本文件 ALL_VIEWS 与 FROZEN（同一次提交）"
    )
