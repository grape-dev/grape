"""前端页面模块缓存版本 ratchet（PR #1231 第 3 轮合规审查 Critical-2）。

回归背景：16 个内容有实质变更的页面模块 ?v= 未 bump，浏览器会拿旧缓存跑新后端。
人工纪律不可靠，本守卫把 index.html 中全部页面模块的 ?v= 冻结成精确映射——
任何版本号变化（含新增模块）都必须同步改这里，改守卫即强制过 review。

纪律（CONTRIBUTING/前端缓存版本约定）：?v= 只升不降；改 JS 必 bump。
"""
import re
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "static" / "index.html"

# 冻结映射：index.html 中 pages 模块的当前版本（2026-11-11 第 3 轮 bump 后快照）
FROZEN = {
    "account.js": "51", "agents.js": "32", "api.js": "32", "app.js": "255",
    "autopilot.js": "74", "auto-recovery.js": "2", "chat.js": "50",
    "dashboard.js": "154", "events.js": "67", "feedback.js": "10",
    "im_channel.js": "31", "issues.js": "141", "knowledge.js": "150",
    "my_feedback.js": "8", "my_feedback_detail.js": "6",
    "observability.js": "78", "organizations.js": "18", "pipeline.js": "50",
    "repos.js": "64", "runtimes.js": "102", "scene-market.js": "51",
    "settings.js": "39", "skills.js": "49", "templates.js": "38",
    "worker-name.js": "2", "pwd-blocklist.js": "3",
}


def test_page_module_cache_versions_match_frozen_map():
    html = INDEX.read_text(encoding="utf-8")
    found = dict(re.findall(r"([A-Za-z_.-]+\.js)\?v=(\d+)", html))
    mismatches = [m for m, v in FROZEN.items()
                  if m not in found or found[m] != v]
    assert not mismatches, (
        f"index.html 页面模块 ?v= 与冻结映射不一致: {mismatches}"
        "（版本只升不降；改 JS 必须 bump 并同步更新 FROZEN）")
    # 冻结映射必须覆盖 index.html 中全部非库模块（新增模块要登记进 FROZEN）
    unregistered = [m for m in found
                    if m not in FROZEN and m not in (
                        "n.js", "faro.js", "iife.js", "name.js",
                        "vue.global.prod.min.js", "jquery.min.js", "marked.min.js",
                        "qrcode.min.js", "echarts.min.js", "sdk.iife.js",
                        "faro-web-sdk.iife.js", "faro-web-tracing.iife.js")
                    and ".min.js" not in m and not m.startswith(".")]
    assert not unregistered, f"新模块未登记进 FROZEN: {unregistered}"
