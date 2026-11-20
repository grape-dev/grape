"""D102-L：设置页第三方账号/仓库凭据职责拆分——行为锁（组件化后口径，2026-11-14）。

方案稿 d102l-settings-split-design.md（v2）+ 2026-11-14 用户复核：
- 用户设置 → 第三方账号（账号安全 tab）：只要 OAuth（共享 BindDialog，allowPat=false）；
- 用户设置 → 凭据管理：只配 PAT（CredRepoRow 行 + PatDialog 配置弹窗）；
- 引导页：OAuth + PAT 都支持（OnboardingView 里 BindDialog allowPat=true）；
- OAuth 设置与 PAT 设置解耦：OAuth 流程只在 BindDialog，PatDialog 不含 OAuth 调用。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ACCT = _ROOT / "src/api/frontend/app/src/views/AccountView.vue"
_APP = _ROOT / "src/api/frontend/shell/js/app.js"
_ONB = _ROOT / "src/api/frontend/app/src/views/OnboardingView.vue"
_BIND = _ROOT / "src/api/frontend/app/src/components/BindDialog.vue"
_ROW = _ROOT / "src/api/frontend/app/src/components/CredRepoRow.vue"
_PAT = _ROOT / "src/api/frontend/app/src/components/PatDialog.vue"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _credentials_tab_segment(src: str) -> str:
    i = src.find("<!-- ═══ credentials ═══ -->")
    assert i >= 0, "凭据 tab 模板缺失"
    j = src.find("Huawei Cloud", i)
    assert j > i, "凭据 tab 与 Huawei Cloud 卡的分界变化"
    return src[i:j]


def test_d102l_security_tab_bind_modal_oauth_only():
    acct = _read(_ACCT)
    i = acct.index("<BindDialog")
    assert ':allow-pat="false"' in acct[i:i + 600], "账号安全页未用纯 OAuth 上下文"
    bind = _read(_BIND)
    assert "!oauthConfigured && !allowPat" in bind, "缺「未开通 OAuth → 指路凭据管理」分支"
    assert "bind_unavailable_hint" in bind and "go_credentials_btn" in bind, "缺指路文案"
    for marker in ("manualTokenInput", "bindTabToken", "manualBindForm"):
        assert marker not in acct, f"账号安全页残留 PAT 入口: {marker}"


def test_d102l_credentials_tab_repo_cred_is_pat_only():
    acct = _read(_ACCT)
    seg = _credentials_tab_segment(acct)
    assert "CredRepoRow" in seg, "凭据行未用组件"
    row = _read(_ROW)
    assert "pat_badge_configured" in row and "pat_badge_none" in row, "缺 PAT 徽章两态"
    assert "pat_btn_configure" in row and "pat_btn_clear" in row, "缺 PAT 配置/清除入口"
    for marker in ("oauthBadge", "oauth_badge_", "oauth_title_", "open_token_page", "token_expired"):
        assert marker not in row, f"凭据行残留 OAuth/过期/令牌页入口: {marker}"


def test_d102l_pat_dialog_targets_settings_credentials():
    pat = _read(_PAT)
    assert "/settings/credentials" in pat, "PAT 弹窗接口不对"
    assert "platform_tokens" in pat, "PAT 载荷不对"
    assert "/auth/bind/" not in pat, "PAT 弹窗混入 OAuth 绑定"


def test_d102k_guide_uses_vcs_catalog_and_supports_pat():
    app = _read(_APP)
    i = app.find("function _renderBindingGuide")
    assert i >= 0, "缺引导页渲染"
    seg = app[i:app.find("function _renderOnboardingShell", i)]
    assert "/settings/vcs/platforms" in seg, "数据源非仓库平台目录"
    assert "configured" in seg, "引导页未接入 OAuth 配置态"
    assert "Promise.all([" in seg, "未并拉配置态"
    onb = _read(_ONB)
    j = onb.index("<BindDialog")
    assert ':allow-pat="true"' in onb[j:j + 600], "引导页未开启 PAT（双模式）"