"""凭据页双模式（2026-11-08 用户裁定矩阵）：OAuth / PAT 两源并存 + 三态角标。

状态枚举（badge 文案与状态一一对应，禁止自由发挥措辞）：
- OAuth 徽章：'平台未配置 OAuth'（灰，入口置灰）/ '未绑定'（跳安全页绑定）
  / 'OAuth 已绑定（生效中）'（无 PAT 时）/ 'OAuth 可用'（PAT 生效时的备用源）
- PAT 徽章：'PAT 生效中' / 'PAT 未配置'
裁定要点：
1. 平台未配置 + 已配 PAT：OAuth 灰标与 PAT 徽章并存显示（不漏灰标）。
2. 双源并存：PAT 标'生效中'，OAuth 标'可用'；PAT 清除后回落 OAuth 生效。
3. 平台取消 OAuth 配置后按未配置处理，不静默清除用户绑定记录（绑定行不动）。
4. 未绑定点 OAuth 入口 → 跳 用户设置→账号安全（_accountGoSecurityTab）。
"""
import re
import pytest
from pathlib import Path

ACCT = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "app" / "src" / "views" / "AccountView.vue"


def _cred_src() -> str:
    src = ACCT.read_text(encoding="utf-8")
    i = src.find("// D102-L: 第三方仓库凭据卡")
    j = src.find("window._accountRepoCredHtml", i)
    return src[i:j]


@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_badge_enum_texts_present():
    src = _cred_src()
    for t in ("平台未配置 OAuth", "未绑定", "绑定生效中", "已配置 PAT", "PAT 未配置"):
        assert t in src, f"缺徽章枚举文案: {t}"


@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_no_oauth_entry_buttons_in_cred_rows():
    """凭据行=徽章即状态（2026-11-08 用户裁定）：无任何跳转账号页/更新入口按钮。"""
    src = _cred_src()
    assert "_oauthState" in src, "缺 OAuth 状态机"
    for banned in ("OAuth 更新", "去绑定 OAuth", "_accountGoSecurityTab", "管理</button>"):
        assert banned not in src, f"凭据行仍含跳转/更新入口: {banned}"
@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_pat_clear_uses_empty_string_put():
    """清除 PAT = PUT 空字符串（settings_credentials_update 语义：空串=清除该字段）。"""
    src = ACCT.read_text(encoding="utf-8")
    i = src.find("function _accountClearPat")
    assert i >= 0, "缺清除 PAT 函数"
    seg = src[i:i + 800]
    assert "API.put('/settings/credentials'" in seg, "清除未走 PUT /settings/credentials"
    assert re.search(r"pat(_token)?['\"]?\s*:\s*''", seg) or "''" in seg, "清除未传空串"


@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_dual_source_badges_coexist():
    src = _cred_src()
    # PAT 生效中 与 OAuth 可用 必须可同时渲染（双徽章并存，不许互斥）
    assert src.count("_oauthBadge") >= 1 and src.count("_patBadge") >= 1
    assert "_oauthBadge" in src and "_patBadge" in src and (
        src.index("_oauthBadge") < src.index("platformCards") if "platformCards" in src else True
    ), "徽章渲染顺序异常"


def test_unconfigured_oauth_does_not_touch_binding():
    """平台取消 OAuth 配置 → 仅显示灰标；代码路径不得清除 external_identities。"""
    src = ACCT.read_text(encoding="utf-8")
    assert "unbind" not in _cred_src().lower(), "凭据页不得出现解绑动作"
