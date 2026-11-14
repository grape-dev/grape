"""凭据页语义（Vue 组件化后）——仓库凭据只配 PAT。

用户裁定（2026-11-08 双模式矩阵 → 2026-11-14 收紧）：**三方账号绑定只允许 OAuth，
三方平台仓库只允许配置 PAT**。因此凭据行（components/CredRepoRow.vue）：
- 只有 PAT 徽章两态（'PAT 未配置' / 'PAT 已配置'），OAuth 三态徽章与 OAuth 过期红标
  全部退出（OAuth 状态属账号安全域）；
- 动作 = 配置/重新配置 + 清除；令牌页直达已按 2026-11-14 裁定从行上去掉（配置弹窗内保留）；
- 行内不得出现绑定/解绑/跳账号页入口（绑定是账号安全页的 OAuth 域）。

PAT 配置弹窗 = components/PatDialog.vue（PUT /settings/credentials，空串=清除走行级 clear）。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
ACCT = _ROOT / "src/api/frontend/app/src/views/AccountView.vue"
ROW = _ROOT / "src/api/frontend/app/src/components/CredRepoRow.vue"
PAT = _ROOT / "src/api/frontend/app/src/components/PatDialog.vue"


def _cred_ui_src() -> str:
    """AccountView 凭据 tab 模板段（第三方仓库凭据卡 → Huawei Cloud 卡之间）。"""
    src = ACCT.read_text(encoding="utf-8")
    i = src.find("<!-- ═══ credentials ═══ -->")
    assert i >= 0, "凭据 tab 模板缺失"
    j = src.find("Huawei Cloud", i)
    assert j > i, "凭据 tab 与 Huawei Cloud 卡分界变化"
    return src[i:j]


def test_pat_badge_enum_texts_present():
    row = ROW.read_text(encoding="utf-8")
    assert "pat_badge_configured" in row and "pat_badge_none" in row, "缺 PAT 徽章两态"
    assert "pat_badge_manual_title" in row and "pat_badge_none_title" in row, "缺 PAT 徽章 title"


def test_no_oauth_badges_in_credential_rows():
    row = ROW.read_text(encoding="utf-8")
    for banned in ("oauthBadge", "oauth_badge_", "oauth_title_", "token_expired"):
        assert banned not in row, f"仓库凭据行残留 OAuth/过期徽章: {banned}"
    acct = ACCT.read_text(encoding="utf-8")
    assert "oauthBadge" not in acct, "AccountView 仍带 OAuth 徽章逻辑"


def test_credential_actions_are_pat_only():
    ui = _cred_ui_src()
    assert "CredRepoRow" in ui, "凭据行未用组件"
    row = ROW.read_text(encoding="utf-8")
    assert "openPatModal" in ui, "缺 PAT 配置入口（行 → AccountView.openPatModal）"
    assert "pat_btn_configure" in row and "pat_btn_clear" in row, "缺 PAT 配置/清除文案"
    assert "open_token_page" not in row, "凭据行不应再有令牌创建页直达（2026-11-14 裁定去掉）"
    for banned in ("bindProvider(", "unbindProvider(", "switchTab('security')"):
        assert banned not in ui + row, f"仓库凭据行出现绑定域动作: {banned}"


def test_pat_write_and_clear_semantics():
    pat = PAT.read_text(encoding="utf-8")
    assert "/settings/credentials" in pat, "PAT 保存接口不对"
    assert "platform_tokens" in pat, "PAT 保存载荷不对"
    src = ACCT.read_text(encoding="utf-8")
    k = src.find("function clearPat(")
    assert k >= 0, "缺 PAT 清除实现"
    clear = src[k:k + 1200]
    assert "/settings/credentials" in clear, "PAT 清除接口不对"
    assert re.search(r"platform_tokens\[[^\]]+\]\s*=\s*''", clear), "清除未走 platform_tokens 空串"


def test_credentials_rows_have_no_unbind_actions():
    row = ROW.read_text(encoding="utf-8").lower()
    assert "unbind" not in row, "凭据行不得出现解绑动作（绑定归账号安全页 OAuth 域）"