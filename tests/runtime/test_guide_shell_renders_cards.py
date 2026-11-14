"""引导页卡片渲染守卫（2026-11-14 引导页 Vue 化后改写到组件源）。

历史背景：沙箱真执行 `_renderOnboardingShell` 抓「孤儿表达式」导致卡片区空白
（2026-11-08 实弹：forEach 内拼接行丢失，语法合法、字符串断言全绿、卡片永远空）。
引导页整页迁 Vue（views/OnboardingView.vue + view chunk onboarding-view.js）后，
壳函数不再拼 DOM，本文件把守卫改成组件源 + 挂载入口两条：
  1. 卡片必须在 `v-for="p in providers"` 的循环体内渲染（循环外渲染 = 卡片数恒 1 的同类事故）；
  2. 卡面结构 = logo + 平台名 + [绑定] 单按钮；无角标残留、无置灰（口径 v4 + 双模式在弹层）；
  3. 入口必须把整份平台目录交给组件（window.renderOnboarding 收 props.providers）。
"""
from pathlib import Path

APP = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "shell" / "js" / "app.js"
VIEW = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "app" / "src" / "views" / "OnboardingView.vue"
ENTRY = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "app" / "src" / "views" / "entries" / "onboarding-view.js"


def _hero_block() -> str:
    src = VIEW.read_text(encoding="utf-8")
    i = src.find('class="ob-hero-bind"')
    assert i >= 0, "缺卡片容器 ob-hero-bind"
    j = src.find("</section>", i)
    return src[i:j]


def test_one_card_per_platform_inside_loop():
    hero = _hero_block()
    assert 'v-for="p in providers"' in hero, "卡片必须在 providers 循环内渲染（防循环外单卡）"
    assert "ob-bind-card" in hero, "缺卡面 class"
    assert hero.count(":key=\"p.provider\"") >= 1, "缺 v-for key（重复渲染风险）"


def test_card_structure_matches_baseline():
    hero = _hero_block()
    for marker in ("ob-bind-icon", "ob-bind-name", "ob-bind-btn", "app.login.ob_bind_btn"):
        assert marker in hero, f"卡面缺基线结构: {marker}"
    assert "ob-status-badge" not in hero, "角标方案已废弃（回潮）"
    assert "disabled" not in hero, "基线卡绑定按钮恒可用（置灰方案废弃）"


def test_cards_not_filtered_by_configured():
    hero = _hero_block()
    assert "filter(" not in hero, "卡片不得按 configured 过滤（未开通 OAuth 也要显示并可配 PAT）"


def test_entry_hands_full_catalog_to_view():
    entry = ENTRY.read_text(encoding="utf-8")
    assert "window.renderOnboarding" in entry, "缺组件挂载入口"
    assert "createApp(OnboardingView" in entry, "入口未挂载 OnboardingView"
    app = APP.read_text(encoding="utf-8")
    i = app.find("function _renderOnboardingShell")
    assert i >= 0
    seg = app[i:app.find("\n  function ", i + 10)]
    assert "window.renderOnboarding(" in seg, "壳函数未把目录交给组件"
    assert "providers: providers || []" in seg, "壳函数未传平台目录"