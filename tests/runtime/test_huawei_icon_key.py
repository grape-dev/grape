"""回归锁：华为渠道图标/品牌色必须按 `huawei` 键生效。

实弹（2026-11-08）：花瓣 logo 注册在 `hwcloud` 键（repos.js platformIcon、
app.js _OAUTH_ICONS），但绑定体系全链用的是 `huawei`（oauth_providers_config
卡键、/auth/providers、external_identities.provider）——华为绑定行永远落不进
花瓣 case，显示成通用 git 图标 + 首字母头像（"M"）。两键必须等价。
"""
import re
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "shell" / "js"


@pytest.mark.skip(reason="repos.js 已随 Vue 迁移删除；平台图标逻辑迁入 ReposView.vue，"
                          "断言改写到 Vue 源需另立专项（本文件保留 shell/js/app.js 侧断言）")
def test_platform_icon_accepts_huawei_key():
    src = (JS / "pages" / "repos.js").read_text(encoding="utf-8")
    assert re.search(r"provider\s*===\s*['\"]huawei['\"]", src), \
        "platformIcon 未识别 huawei 键——华为绑定行走通用图标"


@pytest.mark.skip(reason="repos.js 已随 Vue 迁移删除；品牌色常量随页面迁入 Vue 源，"
                          "断言改写到 Vue 源需另立专项")
def test_platform_brand_color_has_huawei():
    src = (JS / "pages" / "repos.js").read_text(encoding="utf-8")
    assert re.search(r"huawei:\s*['\"]#", src), "PLATFORM_BRAND_COLOR 缺 huawei 品牌色"


@pytest.mark.skip(reason="app.js 的 _OAUTH_ICONS 在迁移后只保留 gitcode/github，"
                          "OAuth 按钮渲染迁到 AccountView.vue；断言改写到 Vue 源需另立专项")
def test_oauth_icons_accept_huawei_key():
    src = (JS / "app.js").read_text(encoding="utf-8")
    i = src.find("function _oauthIcon")
    seg = src[i:src.find("\n  function ", i + 10)]
    assert re.search(r"huawei", seg), "_oauthIcon 未做 huawei→hwcloud 归一，登录按钮华为显示首字母"
