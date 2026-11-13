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
        # 共享运行时模块
        "tokens.css": "1", "app.css": "3", "auth.css": "1",
        "api.js": "35", "app.js": "261",
        "deeplink.js": "1",
        "auto-recovery.js": "2", "oauth-buttons.js": "2",
        "host-status.js": "1", "worker-name.js": "2", "pwd-blocklist.js": "3",
        "faro.js": "7",
        # Vue 视图 chunk
        "dashboard-view.js": "65", "pipeline.js": "50", "agent-view.js": "48",
        "event-view.js": "53", "chat-view.js": "51", "im-channel.js": "49",
        "repos-view.js": "52", "issues-view.js": "56", "account-view.js": "52",
        "scene-market.js": "52", "skills-view.js": "51", "knowledge-view.js": "48",
        "runtimes-view.js": "54", "observability-view.js": "53",
        "organizations.js": "49", "feedback-dialog.js": "49", "my-feedback.js": "49",
        "my-feedback-detail.js": "49", "autopilot-view.js": "50",
    },
    "admin.html": {
        "tokens.css": "1", "pwd-blocklist.js": "3", "admin-view.js": "11",
    },
    "register.html": {
        "app.css": "3", "auth.css": "1", "api.js": "35",
        "pwd-blocklist.js": "3", "auth.js": "7",
    },
    "forgot.html": {
        "app.css": "3", "auth.css": "1", "api.js": "35",
        "pwd-blocklist.js": "3", "auth.js": "7",
    },
}

# 日期型版本模块：交给 i18n_locale_cache_ratchet 的「下界 + 指纹」守护
DATE_VERSIONED = {"i18n.js"}

# 第三方库：版本号随 vendor 打包固定，不参与冻结校验
VENDOR = {
    "vue.global.prod.min.js", "jquery.min.js", "marked.min.js", "qrcode.min.js",
    "echarts.min.js", "select2.min.js", "faro-web-sdk.iife.js",
    "faro-web-tracing.iife.js",
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


def test_register_and_forgot_agree():
    """两封邮件页是同构复制品，版本必须一致（复审 N-1 的跨页分叉）。"""
    reg, fgt = _scan("register.html"), _scan("forgot.html")
    diff = {m: (reg.get(m), fgt.get(m)) for m in set(reg) | set(fgt)
            if reg.get(m) != fgt.get(m) and m not in VENDOR}
    assert not diff, f"register/forgot 版本分叉: {diff}"