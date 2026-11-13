"""登录页/管理面实弹修复系列（weekly-11，2026-11-12）S26-S28 场景用例。

需求：docs/weekly-11/login-whitelist-requirements.md（S26-S28）
设计：docs/weekly-11/login-whitelist-design.md（D19-D20）

全部为实弹暴露的预存 bug 修复资产锁：
- S26 多渠道按钮渲染不崩（孤悬 else 越界）+ 静态预置按钮删除
- S27 github 图标恒白 + 卡 brand_color 归位
- S28 管理面表单对比度 + esc() 本地兜底
"""
import json
import os
import re
from pathlib import Path

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")
os.environ.setdefault("OBS_LOG_DB", "0")
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ROOT = REPO_ROOT / "src"


def _read(rel):
    return (SRC_ROOT / rel).read_text(encoding="utf-8")


# ───────────────── S26：多渠道渲染 + 静态预置删除 ─────────────────

def test_s26_oauth_buttons_dynamic_render():
    """S26：#oauthBtns 容器零静态预置按钮（闪动+与目录漂移源）；
    renderOAuthButtons 无越界形态（孤悬 else 已删，keyed 分支按 onclick 匹配）。"""
    html = _read("api/static/index.html")
    m = re.search(r'id="oauthBtns"[^>]*>(.*?)</div>', html, re.S)
    assert m, "oauthBtns 容器定位失败"
    inner = m.group(1)
    assert "<button" not in inner, "静态预置 OAuth 按钮回潮（闪动源）"
    assert "app.oauthLogin('gitcode')" not in inner

    js = _read("api/static/js/app.js")
    # 孤悬 else 越界形态：existingBtns[j] 访问**没有 onclick 匹配守卫**（keyed
    # 分支的合法形态=先 getAttribute('onclick') 匹配再访问，窗口内有守卫特征）
    lines = js.splitlines()
    for i, line in enumerate(lines):
        if "existingBtns[j].removeAttribute" in line or "existingBtns[j].setAttribute" in line:
            window = "\n".join(lines[max(0, i - 3):i + 1])
            assert ".getAttribute('onclick')" in window, (
                f"existingBtns[j] 裸下标访问回潮（无匹配守卫，孤悬 else 越界 bug 形态）: line {i+1}")
    # 动态渲染链路在位：4 渠道场景 needsRender=true 时走重渲染
    assert "var needsRender = providers.length !== existingBtns.length" in js
    # D108-b：按钮构建器（含 _mkBtn/DOM 模板）迁至同步加载的 oauth-buttons.js
    #（首帧渲染），app.js 经 window.__renderOAuthButtonList 复用同一构建器
    assert "window.__renderOAuthButtonList(container, providers)" in js, (
        "app.js 应委托共享构建器（oauth-buttons.js）做异步对账重排")
    shared_js = _read("api/static/js/oauth-buttons.js")
    assert "container.innerHTML = providers.map(_mkBtn).join('')" in shared_js, (
        "共享构建器应持有按钮 DOM 模板（D108-b 迁移后断言指向）")
    # 修复注释在位（修复出处可追溯：孤悬 else 修复 + 构建器迁移说明）
    assert ("dangling" in js.lower() or "孤悬" in js
            or "dangling" in shared_js.lower() or "孤悬" in shared_js)


# ───────────────── S27：github 图标恒白 + 卡色归位 ─────────────────

def test_s27_github_icon_and_brand_color():
    """S27：github.json brand_color=#24292f；auth.css github 图标恒白
    （!important 压过 light 模式染色）；gitee/huawei 特例不被触碰。"""
    card = json.loads((REPO_ROOT / "configs/platforms/github.json").read_text(encoding="utf-8"))
    assert card.get("brand_color") == "#24292f", f"github 卡品牌色应为 #24292f: {card.get('brand_color')}"

    css = _read("api/static/css/auth.css")
    # github 恒白规则（!important）
    m = re.search(r"\.oauth-btn\.icon-only\[onclick\*=\"oauthLogin\('github'\)\"\] svg \{([^}]*)\}", css)
    assert m, "github 图标恒白规则缺失"
    assert "color: #fff" in m.group(1) and "!important" in m.group(1), "github 白图标规则需 !important（压过 light 模式）"
    # gitee 官方双色特例保留（D105-13 调试成果不被触碰）
    m2 = re.search(r"\.oauth-btn\.icon-only\[onclick\*=\"oauthLogin\('gitee'\)\"\] svg \{([^}]*)\}", css)
    assert m2 and "transparent" in m2.group(1), "gitee 官方双色特例被改坏"
    # 版本 bump（auth.css v>=33）
    idx = _read("api/static/index.html")
    m3 = re.search(r"auth\.css\?v=(\d+)", idx)
    assert m3 and int(m3.group(1)) >= 33, "auth.css 版本未 bump"


# ───────────────── S28：管理面表单对比度 + esc 兜底 ─────────────────

def test_s28_admin_form_contrast_and_esc_fallback():
    """S28：凭据/卡字段输入框显式白底黑字；esc() 有本地兜底
    （admin 页不加载 app.js/auth.js，window.escHtml 不存在不炸）。"""
    html = _read("api/static/admin.html")
    js = _read("api/static/js/admin.js")

    # 凭据输入（静态 HTML）：client_id/client_secret/bot_token/webhook_token
    for fid in ("ac_client_id", "ac_client_secret", "vp_bot_token", "vp_webhook_token"):
        m = re.search(r'id="' + fid + r'"[^>]*style="([^"]*)"', html)
        assert m, f"{fid} 定位失败"
        assert "background:#fff" in m.group(1) and "color:#1d1d1f" in m.group(1), \
            f"{fid} 未钉死白底黑字: {m.group(1)}"
    # 卡字段输入（D21 Tab 版动态渲染）：内联配色（宽度走 CSS 100%）
    assert 'style="background:#fff;color:#1d1d1f"' in js

    # esc() 本地兜底（window.escHtml 缺失时走本地转义链）
    m = re.search(r"function esc\(s\) \{([\s\S]*?)\n  \}", js)
    assert m, "esc() 定位失败"
    body = m.group(1)
    assert "typeof window.escHtml === 'function'" in body, "esc() 缺全局代理守卫"
    assert "replace(/[&<>\"']/g" in body, "esc() 缺本地兜底转义链"
    # 兜底口径：& < > " ' 全转义（与 Auth.escHtml 一致）
    assert "&amp;" in body and "&#39;" in body
