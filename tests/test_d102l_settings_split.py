"""D102-L：设置页第三方账号/仓库凭据职责拆分——行为锁。

方案稿 d102l-settings-split-design.md（v2，评审三修正采纳）。
拆分语义：第三方账号卡=账号绑定（全平台展示绑定/解绑）；仓库 PAT
配置归凭据管理 tab（写入接口 /settings/credentials 原样——入口归位
非搬家）；token 过期告警归凭据 tab；绑定弹窗瘦身为 OAuth-only。
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ACCT = _REPO_ROOT / "src/api/static/js/pages/account.js"


def test_d102l_bind_modal_oauth_only():
    """口径 v6：安全 tab 上下文弹窗仅 OAuth；PAT 页签只在引导页双模式（allowPat）。"""
    src = _ACCT.read_text(encoding="utf-8")
    i = src.find("function _accountBindProvider")
    j = src.find("_accountShowModal(html);", i)
    seg = src[i:j]
    k = seg.find("if (allowPat) {")
    assert k >= 0, "缺 allowPat 上下文分支"
    stabs = seg[k:seg.find("}", k)]
    assert "bindTabOauth" in stabs and "bindTabToken" in stabs, "PAT 页签未收进 allowPat 分支"
    # 安全 tab 上下文 = 最后的 else {} 指路块（无 PAT 表单）；引导页 else-if 分支保留 PAT 直达
    sec = seg[seg.rfind("} else {"):]
    assert "_accountGoCredentialsTab" in sec, "安全 tab 未配 OAuth 应指路凭据管理"
    assert "_accountManualBindForm" not in sec, "安全 tab 未配 OAuth 不应出现 PAT 表单"
    guide = seg[seg.find("} else if (allowPat) {"):seg.rfind("} else {")]
    assert "_accountManualBindForm" in guide, "引导页未配 OAuth 应保留 PAT 表单直达"


def test_d102l_credentials_tab_repo_cred_card():
    """凭据 tab 新增第三方仓库凭据卡（平台清单卡驱动 + 过期红标）。"""
    src = _ACCT.read_text(encoding="utf-8")
    i = src.find("function _accountLoadCredentials")
    seg = src[i:src.find("function _accountOpenPatForm") or i + 6000]
    assert "/settings/vcs/platforms" in seg, "仓库凭据卡未用仓库平台目录"
    assert "token_expired_providers" in seg, "过期态未接入"
    assert "third_party_repo_credentials" in src, "缺 i18n 键"


def test_d102k_guide_dual_path_hint():
    """引导页绑定卡：OAuth 单路径（2026-11-08 用户裁定——绑定归绑定，PAT 归凭据）。

    数据源=仓库平台目录；接入 OAuth 配置态（置灰判定）；卡内不得出现
    PAT 配置入口（_accountOpenPatForm 归凭据 tab 专用）。
    """
    app = (_REPO_ROOT / "src/api/static/js/app.js").read_text(encoding="utf-8")
    i = app.find("function _renderBindingGuide")
    seg = app[i:app.find("function _renderOnboardingShell", app.find("function _renderOnboardingShell", i) + 10)]
    assert "/settings/vcs/platforms" in seg, "数据源非仓库目录"
    assert "oauth_configured" in seg, "引导页未接入 OAuth 配置态"
    # 口径 v3：卡面仅 绑定 按钮；PAT 配置在点击后的弹层里（account.js modal 双模式）
    assert "_accountOpenPatForm" not in seg, "卡面不应有 PAT 按钮（PAT 在弹层内）"
    assert "Promise.all([" in seg, "未并拉配置态"


def test_d102k_oauth_bound_hint_points_to_settings():
    """引导页未绑定提示收敛到角标 title（蓝ⓘ）；跳安全页入口归凭据页双模式。"""
    app = (_REPO_ROOT / "src/api/static/js/app.js").read_text(encoding="utf-8")
    i = app.find("function _renderBindingGuide")
    seg = app[i:app.find("function _renderOnboardingShell", app.find("function _renderOnboardingShell", i) + 10)]
    assert "oauth_configured" in seg, "引导页未接入 OAuth 配置态（口径 v4：双模式在弹层）"
    acct = (_REPO_ROOT / "src/api/static/js/pages/account.js").read_text(encoding="utf-8")
    assert "_accountGoSecurityTab" in acct, "凭据页缺跳安全页入口（去绑定 OAuth）"


def test_d102l_pat_form_reachable_from_credentials():
    """凭据 tab 的 PAT 配置按钮走 _accountOpenPatForm（复用 manual form，
    写入接口 /settings/credentials 原样）。"""
    src = _ACCT.read_text(encoding="utf-8")
    assert "function _accountOpenPatForm" in src
    seg = src[src.find("function _accountOpenPatForm"):
              src.find("function _accountGoCredentialsTab")]
    assert "_accountManualBindForm" in seg, "PAT 表单未复用 manual form"
    # 安全 tab 的过期红卡已迁走（expired 置 false）
    i = src.find("_accountRenderProviderRow(p.name, p.provider, bound, identity, expired)")
    ctx = src[max(0, i - 200):i]
    assert "D102-L" in ctx, "安全 tab 过期告警未迁移（职责4以外错位残留）"
