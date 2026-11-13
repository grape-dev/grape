"""账号安全 tab 第三方账号行三态（D105k 声明制，2026-11-09 用户裁定）。

纯 OAuth 三态：①渠道已配置+OAuth 绑定 → 已绑定+解绑 ②已配置未绑定 → 绑定按钮
③渠道未配置 → 不支持 OAuth（灰ⓘ），绑定/解绑全不渲染。
绑定判定读后端 has_oauth_token 声明制（bool(ei.access_token)，无独立
token_source 字段——旧口径 docstring 谎报已纠正）；
PAT 彻底退出本行（配置/清除/来源均归凭据 tab）。

历史口径（2026-11-08 版"来源：PAT 标签 + patOnly 特殊态"）被用户裁定推翻：
uid=24 gitcode 行实锤——存量 PAT 写在 ei.access_token 使 bool(access_token)
恒 True，"已绑定 来源：PAT"+解绑与 OAuth 域语义自相矛盾。
"""
import re
from pathlib import Path

ACCT = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "app" / "src" / "views" / "AccountView.vue"


def _sec_src() -> str:
    return ACCT.read_text(encoding="utf-8")


def _sec_row_src() -> str:
    """安全 tab 行映射块（var bound = ... 至 }).join('');）。"""
    src = _sec_src()
    i = src.find("var bound = bindings.indexOf")
    j = src.find("}).join('');", i)
    return src[i:j]


def test_security_rows_have_three_state_machine():
    src = _sec_src()
    assert "_secOauthState" in src, "第三方账号行缺三态状态机"
    for t in ("不支持 OAuth", "未绑定", "已绑定"):
        assert t in src, f"缺状态文案: {t}"


def test_bound_requires_declared_oauth_token():
    """D105k 声明制：bound 判定 = has_oauth_token === true（后端按
    bool(ei.access_token) 计算，无 token_source 字段）；!== false 旧推断判据不得回潮。"""
    src = _sec_row_src()
    assert "identity.has_oauth_token === true" in src, \
        "绑定判定未采用声明制 has_oauth_token === true"
    assert "has_oauth_token !== false" not in src, \
        "旧推断判据（非空即 OAuth）残留——存量 PAT 行会被误判已绑定"


def test_unconfigured_row_renders_no_bind_unbind():
    """渠道未配置 → 整行未配置态：传给渲染器的 bound=false，绑定/解绑全不渲染。"""
    src = _sec_row_src()
    # 状态机：configured===false 优先级最高，直接 unconfigured
    assert re.search(r"\(p\.configured === false\)\s*\?\s*'unconfigured'", src), \
        "渠道未配置未收敛为 unconfigured 整行态"
    # 渲染器 bound 参数 = 状态机判定，未配置时恒 False
    assert "_secOauthState === 'bound'" in src, "渲染器 bound 未收敛到状态机"
    renderer = _sec_src()
    i = renderer.find("function _accountRenderProviderRow")
    seg = renderer[i:renderer.find("\nfunction _accountShowChangePassword")]
    # 未绑定/未配置分支不得有解绑按钮（解绑只在 bound 卡片分支）
    unbound_seg = seg[seg.find("// 未绑定"):]
    assert "unbind" not in unbound_seg and "_accountUnbindProvider" not in unbound_seg, \
        "未绑定/未配置行渲染了解绑入口"


def test_pat_exits_security_row():
    """PAT 彻底退出安全 tab 行：无来源小标、无 patOnly 特殊态、无 PAT 文案。"""
    src = _sec_row_src()
    assert "来源：" not in src and "_secCredSource" not in src, \
        "来源：PAT 小标残留——用户裁定 PAT 只归凭据 tab"
    assert "patOnly" not in src, "patOnly 特殊态残留"
    assert "_patOnly" not in src


def test_unconfigured_bind_button_disabled():
    src = _sec_src()
    # 未配置 → 绑定按钮置灰且不可点（aria-disabled，无 onclick 绑定调用）
    assert "aria-disabled" in src, "未配置态绑定按钮未置灰"


def test_badge_attached_to_provider_icon():
    src = _sec_src()
    assert "position:relative" in src and "_secStatusBadge" in src, "状态角标未挂平台图标"
