"""绑定弹窗上下文分域（2026-11-08 口径 v6 职责分域裁定）。

- 第三方账号行（账号安全 tab）= OAuth 域：调用 `_accountBindProvider(provider)`
  （allowPat 缺省 false）→ 弹层仅 OAuth（配置→授权按钮；未配置→指路凭据管理）；
  且行的绑定态只认 OAuth 绑定（PAT 归属记录不算，解绑按钮不出现）。
- 引导页 app.bindProvider → `_accountBindProvider(provider, true)`：双模式
  （OAuth 授权页签 + PAT 配置表单；未配 OAuth → PAT 表单直达）。
- PAT 的一切归 凭据 tab（配置/删除）。
"""
import re
from pathlib import Path

ACCT = Path(__file__).resolve().parent.parent / "src" / "api" / "static" / "js" / "pages" / "account.js"
APP = Path(__file__).resolve().parent.parent / "src" / "api" / "static" / "js" / "app.js"


def test_context_split_signature():
    src = ACCT.read_text(encoding="utf-8")
    assert "function _accountBindProvider(provider, allowPat) {" in src, "缺上下文分域签名"
    assert "allowPat = allowPat === true;" in src, "缺 allowPat 归一"


def test_security_context_oauth_only_modal():
    src = ACCT.read_text(encoding="utf-8")
    i = src.find("function _accountBindProvider")
    j = src.find("_accountShowModal(html);", i)
    seg = src[i:j]
    # stabs 双页签必须在 allowPat 分支内（安全 tab 不渲染 PAT 页签）
    k = seg.find("if (allowPat) {")
    assert k >= 0, "缺 allowPat 分支"
    stabs = seg[k:seg.find("}", k)]
    assert "bindTabOauth" in stabs and "bindTabToken" in stabs, "双页签未收进 allowPat 分支"
    # 未配 OAuth + 非 allowPat（安全 tab）→ 指路凭据管理，无 PAT 表单
    m = seg.find("} else if (allowPat) {")
    assert m >= 0, "缺 else if (allowPat) 分支（引导页 OAuth 未配 → PAT 表单直达）"
    guide = seg[m:seg.find("} else {", m)]
    assert "_accountManualBindForm" in guide, "引导页未配 OAuth 缺 PAT 表单直达"
    sec = seg[seg.rfind("} else {"):]
    assert "_accountGoCredentialsTab" in sec, "安全 tab 未配 OAuth 应指路凭据管理"
    assert "_accountManualBindForm" not in sec, "安全 tab 未配 OAuth 不应出现 PAT 表单"


def test_guide_entry_passes_allowpat_true():
    app = APP.read_text(encoding="utf-8")
    assert "_accountBindProvider(provider, true)" in app, "引导页入口未开双模式"
    assert "_accountBindProvider(provider, false)" not in app, "引导页不应传 false"


def test_security_rows_single_arg_calls():
    acct = ACCT.read_text(encoding="utf-8")
    calls = re.findall(r"_accountBindProvider\([^)]*\)", acct)
    assert len(calls) >= 3, f"安全 tab 绑定调用点应 >=3 个: {calls}"
    for c in calls:
        assert "true" not in c, f"安全 tab 行不应开双模式: {c}"
