"""前端模块缓存版本 ratchet（PR #1231 第 3 轮 Critical-2 / #1363 复审 R-1）。

回归背景：内容有实质变更的模块 ?v= 未 bump，浏览器会拿旧缓存跑新后端。人工纪律不可靠，
本守卫把**四个页面**里全部模块的 ?v= 冻结成精确映射——任何版本号变化（含新增模块）
都必须同步改这里，改守卫即强制过 review。精确等值天然满足「只升不降」。

边界说明：
- 日期型版本的语言包要 `i18n.js`（`?v=20261113-1`）不在本守卫内，由
  `tests/api/test_i18n_locale_cache_ratchet.py` 用「历史下界 + 内容指纹」守护；
- 第三方 vendor 库版本随打包固定，走白名单。
"""
import re
from pathlib import Path

SHELL = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "shell"

# 页面 → {模块: 冻结版本}（2026-11-13 缓存版本回填/统一后快照）
FROZEN = {
    "index.html": {
        "tokens.css": "1", "app.css": "8", "all-views.css": "12", "auth.css": "1", "faro.js": "7",
        "api.js": "36", "pwd-blocklist.js": "3", "worker-name.js": "2", "dashboard-view.js": "75", "pipeline.js": "54",
        "agent-view.js": "50", "event-view.js": "57", "chat-view.js": "60", "im-channel.js": "54", "repos-view.js": "58",
        "issues-view.js": "62", "account-view.js": "59", "scene-market.js": "58", "skills-view.js": "54", "knowledge-view.js": "49",
        "runtimes-view.js": "59", "observability-view.js": "56", "organizations.js": "50", "feedback-dialog.js": "50", "my-feedback.js": "50",
        "my-feedback-detail.js": "50", "autopilot-view.js": "53", "sandbox-view.js": "3", "deeplink.js": "1", "platform-icons.js": "2",
        "app.js": "274", "login-view.js": "2", "onboarding-view.js": "8", "auto-recovery.js": "2", "oauth-buttons.js": "2",
        "host-status.js": "1",
    },
    "admin.html": {
        "admin-view.css": "2", "tokens.css": "1", "pwd-blocklist.js": "3", "admin-view.js": "46",
    },
    "register.html": {
        "app.css": "8", "auth.css": "1", "api.js": "36", "pwd-blocklist.js": "3", "auth.js": "8",
        "register-view.js": "1",
    },
    "forgot.html": {
        "app.css": "8", "auth.css": "1", "api.js": "36", "pwd-blocklist.js": "3", "auth.js": "8",
        "forgot-view.js": "1",
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
