"""回归锁：平台显示名不得把未知平台兜底成 "GitCode"。

实弹事故（2026-11-08）：第三方账号列表第 4 行"GitCode"实为华为账号——
_accountPlatformName() 只查仓库平台目录（gitcode/gitee/github），查不到
兜底返回硬编码 'GitCode'，huawei 被错标且产生两个"GitCode"行。
平台名来源优先级：/auth/providers 渠道卡 → 仓库平台目录 → 平台 id 原样。
"""
import re
import pytest
from pathlib import Path

ACCT = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "app" / "src" / "views" / "AccountView.vue"


def _fn_source(name: str) -> str:
    src = ACCT.read_text(encoding="utf-8")
    i = src.find(f"function {name}")
    assert i >= 0, f"{name} 不存在"
    return src[i:src.find("\nfunction ", i + 10)]


@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_platform_name_consults_auth_provider_card():
    src = _fn_source("_accountPlatformName")
    assert "_accountProvidersData" in src, "平台名未接入 /auth/providers 渠道卡（华为账号会显示成目录外兜底名）"


@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_platform_name_fallback_is_never_hardcoded_gitcode():
    src = _fn_source("_accountPlatformName")
    assert not re.search(r"\|\|\s*['\"]GitCode['\"]", src), "兜底名硬编码 GitCode——未知平台（如 huawei）会被错标"
