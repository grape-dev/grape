"""前端页面模块缓存版本 ratchet（PR #1231 第 3 轮合规审查 Critical-2）。

回归背景：内容有实质变更的页面模块 ?v= 未 bump，浏览器会拿旧缓存跑新后端。
人工纪律不可靠，本守卫把 shell 页面里全部模块的 ?v= 冻结成精确映射——任何版本号
变化（含新增模块）都必须同步改这里，改守卫即强制过 review。

静态根迁移（2026-11-13）：旧根 `src/api/static/` 已并入 `src/api/frontend/shell/`，
本守卫随之指向新根，并把 Vue 视图 chunk（`/static-vue/viewjs/*.js`）一并冻结。

纪律（CONTRIBUTING/前端缓存版本约定）：?v= 只升不降；改 JS 必 bump。
"""
import re
from pathlib import Path

SHELL = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "shell"
INDEX = SHELL / "index.html"

# 冻结映射：shell 页面模块当前版本（2026-11-13 缓存版本回填后快照）
# 注意：只登记纯数字版本；日期型版本的 i18n.js 由 scripts/check-shared-versions.sh 守护。
FROZEN = {
    # 共享运行时模块（多页面消费，见 check-shared-versions.sh SHARED_FILES）
    "api.js": "35", "app.js": "260",
    "auto-recovery.js": "2", "oauth-buttons.js": "2",
    "host-status.js": "1", "worker-name.js": "2", "pwd-blocklist.js": "3",
    "faro.js": "7",
    # Vue 视图 chunk
    "dashboard-view.js": "65", "pipeline.js": "50", "agent-view.js": "48",
    "event-view.js": "53", "chat-view.js": "51", "im-channel.js": "49",
    "repos-view.js": "51", "issues-view.js": "56", "account-view.js": "52",
    "scene-market.js": "52", "skills-view.js": "51", "knowledge-view.js": "48",
    "runtimes-view.js": "54", "observability-view.js": "53",
    "organizations.js": "49", "feedback-dialog.js": "49", "my-feedback.js": "49",
    "my-feedback-detail.js": "49", "autopilot-view.js": "50",
}

# 第三方库：版本号随 vendor 打包固定，不参与冻结校验
VENDOR = {
    "vue.global.prod.min.js", "jquery.min.js", "marked.min.js", "qrcode.min.js",
    "echarts.min.js", "select2.min.js", "faro-web-sdk.iife.js",
    "faro-web-tracing.iife.js",
}


def test_page_module_cache_versions_match_frozen_map():
    html = INDEX.read_text(encoding="utf-8")
    found = dict(re.findall(r"([A-Za-z_.\-/]+\.js)\?v=(\d+)(?=[\"'])", html))
    found = {m.rsplit("/", 1)[-1]: v for m, v in found.items()}

    mismatches = [f"{m}: frozen={FROZEN[m]}, html={found.get(m)}"
                  for m in FROZEN if found.get(m) != FROZEN[m]]
    assert not mismatches, (
        f"shell/index.html 模块 ?v= 与冻结映射不一致: {mismatches}"
        "（版本只升不降；改 JS 必须 bump 并同步更新 FROZEN）"
    )

    unregistered = sorted(m for m in found
                          if m not in FROZEN and m not in VENDOR
                          and not m.startswith("."))
    assert not unregistered, f"新模块未登记进 FROZEN: {unregistered}"