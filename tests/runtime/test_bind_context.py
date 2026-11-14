"""绑定弹窗上下文分域（D102-L v6，2026-11-14 复核 + 组件化后）。

用户裁定（2026-11-08，2026-11-14 多次明示）：
- **引导页**：OAuth + PAT 两条路径都支持——已开通 OAuth → [OAuth 授权] 页签 + [手动令牌] 页签；
  未开通 OAuth 的渠道 → 弹层里直接给 PAT 表单（新用户没有 OAuth 配置也能起步）。
- **用户设置 → 账号安全（第三方账号行）**：只要 OAuth——未开通 OAuth 的渠道渲染灰态 +
  指路「凭据管理」，绝不出现 PAT 表单。
- **用户设置 → 凭据管理**：只配 PAT（见 tests/mgmt/test_credentials_dual_mode.py）。
- OAuth 设置与 PAT 设置解耦：OAuth 流程只在 BindDialog，PAT 弹窗（PatDialog）不含 OAuth 调用。

实现形态（2026-11-14）：绑定弹窗抽成共享 Vue 组件 `components/BindDialog.vue`（allowPat 开关），
引导页 = views/OnboardingView.vue（allowPat=true，chrome=shell 保持原弹层观感），
账号安全页 = AccountView.vue（allowPat=false）；app.js 字符串拼 DOM 的绑定弹层已删，只留兼容入口。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "src/api/frontend/shell/js/app.js"
ACCT = ROOT / "src/api/frontend/app/src/views/AccountView.vue"
ONB = ROOT / "src/api/frontend/app/src/views/OnboardingView.vue"
BIND = ROOT / "src/api/frontend/app/src/components/BindDialog.vue"
PAT = ROOT / "src/api/frontend/app/src/components/PatDialog.vue"


def _bind() -> str:
    return BIND.read_text(encoding="utf-8")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_bind_dialog_splits_by_allowpat():
    src = _bind()
    assert "allowPat" in src, "缺 allowPat 上下文开关"
    assert "oauthConfigured && allowPat" in src, "引导页缺双页签渲染条件"
    assert "manual_token" in src, "缺 PAT 页签字样"
    assert "!oauthConfigured && !allowPat" in src, "缺安全页「未开通 OAuth」分支"
    assert "bind_unavailable_hint" in src and "go_credentials_btn" in src, "缺指路凭据管理文案"
    assert "app.nav('account', 'credentials')" in src, "指路入口未走 nav（壳内无 hashchange）"


def test_bind_dialog_keeps_both_paths_decoupled():
    src = _bind()
    assert "/auth/bind/" in src, "OAuth 授权未走 /auth/bind"
    assert "/settings/credentials" in src, "PAT 保存未走 /settings/credentials"
    assert "platform_tokens" in src, "PAT 载荷不对"
    assert "startOauth" in src and "savePat" in src, "OAuth/PAT 未分成两条独立路径"


def test_security_tab_uses_oauth_only_context():
    src = _read(ACCT)
    assert "<BindDialog" in src, "账号安全页未用共享绑定组件"
    i = src.index("<BindDialog")
    seg = src[i:i + 600]
    assert ':allow-pat="false"' in seg, "账号安全页必须传 allowPat=false（纯 OAuth）"
    for marker in ("manualTokenInput", "bindTabToken", "manualBindForm"):
        assert marker not in src, f"账号安全页仍残留 PAT 表单: {marker}"


def test_security_rows_hide_actions_for_unconfigured_channel():
    src = _read(ACCT)
    i = src.find('v-if="!channelConfigured(p.provider)"')
    assert i >= 0, "第三方账号行缺「未开通 OAuth」分支"
    seg = src[i:src.find("<!-- token 过期 -->", i)]
    assert "openBind(" not in seg, "未开通渠道不应渲染绑定入口"
    assert "unbindProvider(" not in seg, "未开通渠道不应渲染解绑入口"
    assert "bind_unavailable_badge" in seg and "bind_unavailable_hint" in seg, "缺灰态说明"


def test_guide_page_uses_pat_enabled_context():
    src = _read(ONB)
    assert "<BindDialog" in src, "引导页未用共享绑定组件"
    i = src.index("<BindDialog")
    seg = src[i:i + 600]
    assert ':allow-pat="true"' in seg, "引导页必须传 allowPat=true（OAuth + PAT 双模式）"
    assert 'chrome="shell"' in seg, "引导页弹层应沿用壳弹层外观（样式与之前一致）"


def test_shell_keeps_only_compat_entry():
    src = _read(APP)
    assert "_bindManualForm" not in src, "壳里仍残留 PAT 表单字符串拼 DOM"
    assert "_bindDoManual" not in src, "壳里仍残留手动令牌提交路径"
    assert "__grapeOnboardingOpenBind" in src, "壳侧缺组件兼容入口"


def test_pat_save_refreshes_relation_snapshot():
    """PAT 保存后必须刷新「用户↔仓库平台关系」快照。

    关系行是服务端写（hooks.on_pat_saved），前端 __repoPlatformLinks 是登录时快照；
    不刷新就会被引导门按旧快照拦回引导页（实弹：配完 PAT 仍显示引导页 / 不进仓库）。
    """
    app = _read(APP)
    assert "function refreshRepoLinks(" in app, "shell 缺关系刷新函数"
    assert "refreshRepoLinks," in app, "refreshRepoLinks 未导出到 app.*"
    assert "/auth/platform-links" in app, "关系刷新未走 /auth/platform-links"
    bind = _bind()
    assert "refreshRelations" in bind, "绑定弹层保存后未刷新关系"
    onb = _read(ONB)
    assert "refreshRepoLinks" in onb, "引导页绑定成功后未刷新关系"
    acct = _read(ACCT)
    assert "refreshRepoLinks" in acct, "凭据页保存 PAT 后未刷新关系"


def test_pat_dialog_is_pat_only():
    src = _read(PAT)
    assert "/settings/credentials" in src, "PAT 弹窗未走凭据接口"
    assert "/auth/bind/" not in src, "PAT 弹窗混入 OAuth 绑定调用"
    assert "oauthBindBtn" not in src, "PAT 弹窗混入 OAuth 按钮"