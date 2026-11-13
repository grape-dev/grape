"""引导页绑定卡样式回归基线（2026-11-08 口径 v4 用户裁定）。

卡 = logo + 平台名 + [绑定] 单按钮（与基线 arch-optimization 完全一致）；
双模式（OAuth 授权 / PAT 配置）在点击后的弹层内（account.js modal，
OAuth 已配置 → 双页签；未配置 → 仅 PAT 表单）。角标方案已废弃。
"""
from pathlib import Path

APP = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "shell" / "js" / "app.js"
ACCT = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "app" / "src" / "views" / "AccountView.vue"


def _shell_src():
    src = APP.read_text(encoding="utf-8")
    i = src.find("function _renderOnboardingShell")
    j = src.find("\n  function ", i + 10)
    return src[i:j]


def test_card_matches_baseline_structure():
    src = _shell_src()
    assert "ob-status-badge" not in src, "角标方案已废弃（回潮）"
    assert src.count("app.login.ob_bind_btn") >= 1, "缺 [绑定] 按钮"


def test_modal_dual_mode_on_configured():
    acct = ACCT.read_text(encoding="utf-8")
    i = acct.find("function _accountBindProvider")
    seg = acct[i:acct.find("\nfunction _accountOpenPatForm", i)]
    assert "bindTabOauth" in seg and "bindTabToken" in seg, "OAuth 已配置弹层缺双页签"
    assert "_accountManualBindForm" in seg, "弹层缺 PAT 表单"


def test_unconfigured_modal_pat_only():
    """口径 v6：引导页（allowPat）OAuth 未配 → PAT 表单直达；安全 tab 上下文指路凭据。"""
    acct = ACCT.read_text(encoding="utf-8")
    i = acct.find("} else if (allowPat) {", acct.find("function _accountBindProvider"))
    assert i >= 0, "缺引导页 PAT 直达分支"
    seg = acct[i:acct.find("} else {", i)]
    assert "_accountManualBindForm(provider, isZh, pName, false)" in seg, "引导页未配 OAuth 缺 PAT 表单"


