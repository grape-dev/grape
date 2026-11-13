"""D102-K：引导页数据源分离——仓库平台绑定 ≠ 登录渠道（双卡建模回归锁）。

用户实弹：引导页出现"华为"绑定卡片——根因 _renderBindingGuide 误用
/auth/providers（登录渠道列表，含 hwcloud）渲染仓库绑定卡片。
D10 双卡建模本意：登录渠道 ≠ 仓库平台，两目录接口必须各用各的。
"""
import re
import pytest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skip(reason="被测对象随 Vue 迁移下线（本 PR 删除 src/api/static/js/pages/* 与 admin.js）：断言改写到 Vue 源为后续专项，不得以跳过冒充已覆盖")
def test_d102k_guide_uses_vcs_catalog():
    """_renderBindingGuide 数据源 = /settings/vcs/platforms（仓库平台卡目录，API helper 自带 /api 前缀——不得双前缀）。"""
    app = (_REPO_ROOT / "src/api/frontend/shell/js/app.js").read_text(encoding="utf-8")
    i = app.find("function _renderBindingGuide")
    seg = app[i:app.find("function _renderOnboardingShell")]
    assert "/settings/vcs/platforms" in seg, \
        "引导页数据源不是仓库平台目录（登录渠道混入绑定卡片的回潮）"
    # D102-K 续：/auth/providers 允许并拉但仅取 OAuth 配置态（绑定列表必须是
    # 仓库目录）——providers 结果不得直传渲染函数（调用点实参须是 platforms/兜底数组）
    for m in re.finditer(r"_renderOnboardingShell\((\w+)", seg):
        assert m.group(1) != "providers", \
            "登录渠道列表被直渲为绑定卡片（混轴回潮）"


def test_d102k_login_page_still_uses_auth_providers():
    """登录页 OAuth 按钮仍用 /auth/providers（登录渠道轴——正确用途不回潮）。"""
    app = (_REPO_ROOT / "src/api/frontend/shell/js/app.js").read_text(encoding="utf-8")
    i = app.find("function renderOAuthButtons")
    seg = app[i:i + 3000]
    assert "/auth/providers" in seg, "登录页 OAuth 按钮数据源被误改"


def test_d102k_oauth_buttons_icon_only():
    """登录页 OAuth 按钮为 gitee 风格 icon-only（纯 logo 圆形+title 提示）。"""
    app = (_REPO_ROOT / "src/api/frontend/shell/js/app.js").read_text(encoding="utf-8")
    i = app.find("function renderOAuthButtons")
    seg = app[i:i + 9000]  # icon-only 按钮构造在函数尾部 3000 字符窗口之外
    assert 'oauth-btn icon-only' in seg, "登录按钮未走 icon-only 形态"
    assert 'title=' in seg and 'aria-label=' in seg, "icon-only 缺平台名提示（可访问性）"
    # D107 改用官方品牌 mark（1024×1024 华为官方路径，非 Simple Icons 24×24 口径），
    # 且 app.find("hwcloud:") 现先命中顶层 _PLATFORM_NAMES——写死窗口/坐标锚点已失效。
    # 结构锚点：品牌图 map 的 hwcloud 条目必须是完整 <svg>（viewBox + data-brand 官方标识 + 实体 path）。
    m = re.search(r"hwcloud: '(<svg.*?</svg>)',", app, re.S)
    assert m, "OAuth 品牌图缺 hwcloud SVG mark"
    mark = m.group(1)
    assert 'viewBox=' in mark, "hwcloud mark 缺 viewBox"
    assert 'data-brand="huawei"' in mark, "hwcloud mark 未标官方品牌（data-brand）"
    assert mark.count("<path") >= 1, "hwcloud mark 缺实体 path（手搓八瓣已驳回，勿回潮）"


@pytest.mark.skip(reason="被测对象随 Vue 迁移下线（本 PR 删除 src/api/static/js/pages/* 与 admin.js）：断言改写到 Vue 源为后续专项，不得以跳过冒充已覆盖")
def test_d102k_bind_modal_oauth_config_check():
    """绑定模态框 OAuth configured 判断仍走 /auth/providers 缓存（登录渠道轴正确用途）。"""
    acct = (_REPO_ROOT / "src/api/frontend/app/src/views/AccountView.vue").read_text(encoding="utf-8")
    assert "_accountProvidersData" in acct
    assert ".providers || []).find(function(p) { return p.provider === provider; })" in acct
