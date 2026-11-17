"""Issue #3081 回归锁：按钮尺寸单一源（四代叠加 / 容器派生外溢）。

背景（线上 CSS 实测）：app.css 里四代 `.bt*` 尺寸规则叠在 `!important` 上互相覆盖，
最新一代（12px / 4-14 / 26px）整段是死代码；`:root .cd .bt-p` 与 `:root .srow ~ .bt`
按容器位置把卡内小号主按钮放大到 36px。定稿口径：

1. 尺寸只由 class 决定（`.bt` / `.bt.bt-s` / `.bt.bt-xs`），值走 CSS 变量；
2. 容器/位置规则（`.cd .bt-p`、`.srow ~ .bt`）不得声明 padding/font-size/min-height；
3. 模板里 `.bt*` 按钮不得用内联 font-size/padding（禁止第四/第五代补丁）；
4. 无颜色修饰的裸 `.bt` / `bt bt-s` 不得存在（否则落回 UA 灰底）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = ROOT / "src" / "api" / "frontend" / "app" / "src" / "styles" / "global" / "app.css"
APP = ROOT / "src" / "api" / "frontend" / "app" / "src"


def _css() -> str:
    return CSS.read_text(encoding="utf-8", errors="ignore")


def _blocks(css: str, selector_re: str):
    """按 selector 匹配返回 (selector, body) 列表（简易块解析，够用于本守卫）。"""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sel, body = m.group(1).strip(), m.group(2)
        if re.search(selector_re, sel):
            out.append((sel, body))
    return out


def test_container_rules_have_no_size_declarations():
    css = _css()
    for sel_re in (r"\.cd \.bt-p", r"\.srow ~ \.bt"):
        for sel, body in _blocks(css, sel_re):
            lowered = body.lower()
            assert "padding" not in lowered, f"{sel} 仍声明 padding（容器派生尺寸）"
            assert "font-size" not in lowered, f"{sel} 仍声明 font-size"
            assert "min-height" not in lowered, f"{sel} 仍声明 min-height"


def test_size_vars_and_three_tiers_exist():
    css = _css()
    for var in ("--btn-h", "--btn-h-sm", "--btn-h-xs",
                "--btn-pad-x", "--btn-pad-x-sm", "--btn-pad-x-xs",
                "--btn-fs", "--btn-fs-sm", "--btn-fs-xs"):
        assert var in css, f"单一源缺少变量 {var}"
    for tier in (r"\.bt\b", r"\.bt\.bt-s", r"\.bt\.bt-xs"):
        blocks = _blocks(css, tier)
        assert any("var(--btn-h" in b for _, b in blocks), f"{tier} 未使用尺寸变量"


def test_templates_have_no_inline_button_sizes():
    bad = []
    for f in APP.rglob("*.vue"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'<button[^>]*class="[^"]*\bbt\b[^"]*"[^>]*style="([^"]*)"', text, re.S):
            style = m.group(1)
            if re.search(r"(font-size|padding)\s*:", style):
                bad.append(f"{f.relative_to(ROOT)}: {style[:60]}")
    assert not bad, "按钮仍带内联尺寸（应改用 .bt / .bt-s / .bt-xs）：\n" + "\n".join(bad)


def test_no_uncoloured_buttons():
    import json
    colours = ("bt-p", "bt-o", "bt-r", "bt-g", "bt-tg")
    bad = []
    for f in APP.rglob("*.vue"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'<button[^>]*class="([^"]*)"', text, re.S):
            classes = m.group(1).split()
            if "bt" not in classes:
                continue
            if not any(c in classes for c in colours):
                bad.append(f"{f.relative_to(ROOT)}: {' '.join(classes)}")
    assert not bad, "存在无颜色修饰的 .bt 按钮（会落回 UA 灰底）：\n" + "\n".join(bad)


def test_row_container_single_source():
    """第 5 条：行容器尺寸单一源（ac__prow / cred-row 共用 §23c 变量）。

    回归背景：`.ac__prow` 14·16 vs `.cred-row` 12·16，同页两张卡行高永久差 4px。
    定稿口径：尺寸只在 app.css §23c 的共用选择器里声明一次，scoped 侧不得再声明。
    """
    css = _css()
    for var in ("--row-pad-y", "--row-pad-x", "--row-radius", "--row-gap", "--row-min-h"):
        assert var in css, f"行容器单一源缺少变量 {var}"
    shared = re.search(r":root \.ac__prow,\s*:root \.cred-row\s*\{([^}]*)\}", css)
    assert shared, "app.css 缺少 .ac__prow / .cred-row 共用行容器规则"
    body = shared.group(1)
    for prop in ("min-height", "padding", "gap", "border-radius"):
        assert prop in body, f"行容器单一源缺少 {prop}"
    # scoped 侧不得再各自声明尺寸（否则又退回两套）
    for rel, cls in (("views/AccountView.vue", r"\.ac__prow \{"),
                     ("components/CredRepoRow.vue", r"\.cred-row \{")):
        m = re.search(cls + r"([^}]*)\}", (APP / rel).read_text(encoding="utf-8"))
        if m:
            scoped_body = m.group(1)
            for prop in ("padding", "min-height", "border-radius"):
                assert prop not in scoped_body, f"{rel} 的 {cls} 仍声明 {prop}（应交给 §23c）"


def test_account_row_four_states_have_avatar_slot():
    """第 6 条：四态等高 —— 每个状态行都要有头像占位（grid 第一列），否则列错位。

    过期态原先直接以内容 + 按钮开头（2 列），grid 化后第一列会被内容占住，
    与其它三态的头像列错位；这里把「过期态必须有 avatar」锁死。
    """
    text = (APP / "views" / "AccountView.vue").read_text(encoding="utf-8")
    start = text.index('class="ac__prow ac__prow--expired"')
    tail = text[start:]
    end = tail.index('v-else-if="isBound(p.provider)"')
    block = tail[:end]
    assert "ac__avatar" in block, "过期态缺少头像占位（四态 grid 第一列会错位）"
