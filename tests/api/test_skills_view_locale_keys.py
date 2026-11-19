"""SkillsView 文案 key 命名空间守卫（QA D1 复审发现项）。

回归背景（2026-11-19 PR #1743 第 2 轮复审）：40 个新增文案 key 被放进
`shell/locales/{zh,en}/skills.json` 的 `huawei` 子对象，而页面经 `tt()` 读
`skills.<key>` **顶层扁平 key**（`skills.huawei.<key>` 只服务 `th()` 的 9 个
华为子技能文案）——`I18N.t` 按精确点路径查找、不跨前缀回落，英文界面全部
回落到 `KEY_DEFAULTS` 的中文内联串。

本守卫把三件事钉死：
  1. `SkillsView.vue` 里 `KEY_DEFAULTS` 的全部 key 在 zh/en 两份 skills.json
     的**顶层**都必须存在（不查嵌套对象——`tt()` 读不到嵌套）；
  2. 顶层 value 必须是字符串（`tt()` 直接渲染，对象会导致界面输出 [object Object]）；
  3. `huawei` 子对象只允许既有 9 个华为子技能 key（防止新文案再误入该命名空间）。

纪律：新增 tt()/tt2() 文案 → key 进 KEY_DEFAULTS 的同一次提交内补进
zh/en 顶层，并 bump 四页 cacheVer、重算 LOCALE_FINGERPRINT（i18n 棘轮）。
"""
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VUE = REPO / "src" / "api" / "frontend" / "app" / "src" / "views" / "SkillsView.vue"
LOCALES = REPO / "src" / "api" / "frontend" / "shell" / "locales"

# th()（skills.huawei.*）的既有文案，历史遗留命名空间：只减不增
HUAWEI_KEYS = {
    "package_name", "loading_sub", "loading", "files_count", "back",
    "no_detail", "no_sub_skill", "no_content", "reference_docs",
}


def _key_defaults() -> list[str]:
    text = VUE.read_text(encoding="utf-8")
    m = re.search(r"const KEY_DEFAULTS = \{(.*?)\n\}", text, re.S)
    assert m, "SkillsView.vue 未找到 KEY_DEFAULTS"
    keys = re.findall(r"^\s{2}([a-z0-9_]+):", m.group(1), re.M)
    assert keys, "KEY_DEFAULTS 解析为空"
    return keys


def _load(lang: str) -> dict:
    return json.loads((LOCALES / lang / "skills.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_key_defaults_resolvable_at_toplevel(lang: str) -> None:
    """tt() 读 skills.<key> 顶层：KEY_DEFAULTS 每个 key 顶层必须存在且为字符串。"""
    data = _load(lang)
    missing = [k for k in _key_defaults() if k not in data]
    assert not missing, (
        f"skills.json({lang}) 顶层缺 KEY_DEFAULTS key：{missing}——"
        "tt()/tt2() 按 skills.<key> 顶层查找（不进 huawei 子对象），"
        "缺失时英文界面会回落中文内联串（QA D1）"
    )
    bad_type = {k: data[k] for k in _key_defaults()
                if not isinstance(data[k], str)}
    assert not bad_type, f"skills.json({lang}) 顶层文案必须是字符串: {bad_type}"


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_huawei_namespace_frozen(lang: str) -> None:
    """huawei 子对象是 th() 的历史遗留命名空间，只许保留 9 个既有 key，禁止新增。"""
    hw = _load(lang).get("huawei") or {}
    extra = sorted(set(hw) - HUAWEI_KEYS)
    assert not extra, (
        f"skills.json({lang}).huawei 出现新 key：{extra}——"
        "tt() 文案应放顶层（tt() 读不到 huawei.*），huawei 只服务 th() 的华为子技能文案"
    )
