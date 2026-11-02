"""Issue #693: dashboard 智能推荐（场景推荐配置）3 个 i18n 文案键缺失——修复验证测试。

覆盖：
- dashboard.js 引用 recommend_load_failed / recommend_no_scenes / recommend_scenes_loading
  三键，zh/en 语言包此前均缺失，导致静态 i18n 一致性守卫长期报红。
- 修复后：zh/en dashboard.json 均含 3 键，且 zh/en 键集合完全一致。

测试方式：源码 + 语言包检查（前端 JS 无法直接在 pytest 中执行，与
tests/test_issue_479_scene_label.py 同风格）。
"""
import json
import os
import re

import pytest

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "api", "static")

REQUIRED_KEYS = (
    "recommend_load_failed",
    "recommend_no_scenes",
    "recommend_scenes_loading",
)


def _read_file(rel_path):
    full = os.path.join(_STATIC_DIR, rel_path)
    with open(full, encoding="utf-8") as f:
        return f.read()


def _load_locale(lang):
    return json.loads(_read_file(f"locales/{lang}/dashboard.json"))


class TestDashboardRecommendI18nKeys:
    """T1: 3 个 recommend_* 键在 zh/en 语言包均存在。"""

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_t1_01_required_keys_exist(self, lang):
        locale = _load_locale(lang)
        missing = [k for k in REQUIRED_KEYS if k not in locale]
        assert not missing, (
            f"locales/{lang}/dashboard.json 缺失智能推荐文案键: {missing}"
        )

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_t1_02_required_keys_have_nonempty_text(self, lang):
        locale = _load_locale(lang)
        for k in REQUIRED_KEYS:
            assert locale.get(k), f"locales/{lang}/dashboard.json 的 {k} 文案为空"


class TestLocaleParity:
    """T2: zh/en 语言包键集合完全一致（i18n 全覆盖前提）。"""

    def test_t2_01_zh_en_key_sets_identical(self):
        zh = _load_locale("zh")
        en = _load_locale("en")
        assert set(zh.keys()) == set(en.keys()), (
            "zh/en dashboard.json 键集合不一致"
        )


class TestDashboardsJsConsumesKeys:
    """T3: dashboard.js 仍引用这 3 键（防止误删消费侧）。"""

    def test_t3_01_dashboard_js_references_required_keys(self):
        src = _read_file("js/pages/dashboard.js")
        for k in REQUIRED_KEYS:
            assert f"I18N.t('dashboard.{k}')" in src, (
                f"dashboard.js 已不再引用 dashboard.{k}——若为有意移除，应同步清理本测试"
            )
