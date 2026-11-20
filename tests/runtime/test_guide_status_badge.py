"""引导页绑定卡与弹层守卫（2026-11-14 引导页 Vue 化 + 双模式口径）。

卡面基线（2026-11-08 用户裁定 v4）：logo + 平台名 + [绑定] 单按钮，无角标、不置灰。
点击后弹层（共享 BindDialog，chrome=shell 保持原壳弹层外观）：
  - 已开通 OAuth → [OAuth 授权](默认) + [手动令牌] 双页签；
  - 未开通 OAuth → PAT 表单直达（引导页必须能配 PAT 起步）。
账号安全页用同一组件但 allowPat=false（纯 OAuth，未开通渠道指路凭据管理）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "src/api/frontend/shell/js/app.js"
VIEW = ROOT / "src/api/frontend/app/src/views/OnboardingView.vue"
BIND = ROOT / "src/api/frontend/app/src/components/BindDialog.vue"
ACCT = ROOT / "src/api/frontend/app/src/views/AccountView.vue"


def _hero() -> str:
    src = VIEW.read_text(encoding="utf-8")
    i = src.find('class="ob-hero-bind"')
    assert i >= 0, "缺卡片容器"
    return src[i:src.find("</section>", i)]


def test_card_matches_baseline_structure():
    hero = _hero()
    assert "ob-status-badge" not in hero, "角标方案已废弃（回潮）"
    assert "app.login.ob_bind_btn" in hero, "缺 [绑定] 按钮"
    assert 'v-for="p in providers"' in hero, "缺平台循环"


def test_guide_card_is_dual_mode_entry():
    hero = _hero()
    assert "disabled" not in hero, "引导页卡片不应置灰（OAuth 未开通也要能配 PAT）"
    assert "ob-bind-card--off" not in hero, "引导页卡片不应有灰态 class"
    assert '@click="openBind(' in hero, "卡片缺绑定入口"


def test_guide_bind_modal_is_dual_mode():
    src = BIND.read_text(encoding="utf-8")
    assert "allowPat" in src, "缺 allowPat 上下文（引导页双模式 / 安全 tab 纯 OAuth）"
    assert "manual_token" in src, "引导页缺 PAT 页签文案"
    assert "pat_no_refresh_hint" in src, "未开通 OAuth 时缺 PAT 提示"
    assert "startOauth" in src, "缺 OAuth 授权动作"


def test_bind_modal_points_to_credentials_without_oauth():
    src = BIND.read_text(encoding="utf-8")
    assert "bind_unavailable_hint" in src and "go_credentials_btn" in src, "未开通 OAuth 缺指路说明"
    assert "app.nav('account', 'credentials')" in src, "指路入口未指向凭据管理 tab（壳内无 hashchange，必须走 nav）"


def test_guide_modal_keeps_shell_chrome():
    src = VIEW.read_text(encoding="utf-8")
    i = src.index("<BindDialog")
    assert 'chrome="shell"' in src[i:i + 600], "引导页弹层应沿用壳弹层外观（440px 卡片）"


def test_account_security_bind_modal_is_oauth_only():
    src = ACCT.read_text(encoding="utf-8")
    i = src.index("<BindDialog")
    seg = src[i:i + 600]
    assert ':allow-pat="false"' in seg, "账号安全页必须是纯 OAuth 上下文"
    assert "manualTokenInput" not in src and "bindTabToken" not in src, "账号安全页混入 PAT 页签"


def test_guide_mount_delegation():
    app = APP.read_text(encoding="utf-8")
    i = app.find("function _renderOnboardingShell")
    seg = app[i:app.find("\n  function ", i + 10)]
    assert "window.renderOnboarding(" in seg, "壳未把渲染交给 Vue 组件"
    assert "providers: providers || []" in seg, "壳未传平台目录"