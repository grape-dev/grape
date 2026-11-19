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


# 概览槽位（#3227）：tt('ov_' + k) 的动态键前缀——槽位集固定，展开校验
_OV_SLOTS = ("scope", "steps", "products", "constraints", "review")


def _key_defaults() -> list[str]:
    """需要顶层可解析的全部文案键：KEY_DEFAULTS + tt()/tt2() 字面量 + ov_ 槽位展开。

    覆盖面（QA 第 4 轮 Informational 2 建议的扩展）：
    - KEY_DEFAULTS 全量 key（原有覆盖）；
    - tt('...') / tt2('...') 的全部单引号字面量（含不在 KEY_DEFAULTS 的键，
      如 scope_official——I18N 已有该键，但 KEY_DEFAULTS 没有兜底条目）；
    - tt('ov_' + k) 动态键按槽位集展开为 ov_scope…ov_review。
    """
    text = VUE.read_text(encoding="utf-8")
    keys: list[str] = []
    m = re.search(r"const KEY_DEFAULTS = \{(.*?)\n\}", text, re.S)
    assert m, "SkillsView.vue 未找到 KEY_DEFAULTS"
    kd = re.findall(r"^\s{2}([a-z0-9_]+):", m.group(1), re.M)
    assert kd, "KEY_DEFAULTS 解析为空"
    keys += kd
    literals = re.findall(r"""tt2?\(['"]([a-z0-9_]+)['"]\)""", text)
    keys += [k for k, _ in ((l, 0) for l in literals)]
    keys += [f"ov_{s}" for s in _OV_SLOTS]
    seen, uniq = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k); uniq.append(k)
    return uniq


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
