"""管理台页面 i18n 词条守卫：代码里用的 `page.*` key 必须在 zh/en 语言包里存在。

回归背景（2026-11-20 实锤）：`平台卡（高级）` 区块只写了
`t('page.card_what', '中文兜底')`，语言包没有该 key。`t()` 在语言包缺失时静默回退
到第二参，中文环境看起来完全正常，**英文界面直接显示中文**——语言包棘轮只守
「内容变了要 bump 版本」，不守「代码用到的 key 是否存在」，于是这类漏翻一路绿灯。

本守卫把「视图里出现的字面 key」与「两份语言包的键集」绑死：缺一个就红。
边界：只扫字面 key —— `t('page.col_' + k)` / `t('page.health_' + s)` 这类拼前缀的
动态用法不扫（反向「死词条」也不扫：本文件有大量前缀拼接键，硬扫会全是假红）。
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOCALES = ROOT / "src" / "api" / "frontend" / "shell" / "locales"
VIEWS = ROOT / "src" / "api" / "frontend" / "app" / "src" / "views"

# t('page.some_key' 与 t('page.some_key', '兜底文案') 两种形态都要覆盖
_KEY = re.compile(r"t\(\s*'page\.([a-z0-9_]+)'")


def _locale_page_keys(lang: str) -> set[str]:
    data = json.loads((LOCALES / lang / "admin.json").read_text(encoding="utf-8"))
    return set((data.get("page") or {}).keys())


def _used_page_keys() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for vue in sorted(VIEWS.glob("*.vue")):
        for m in _KEY.finditer(vue.read_text(encoding="utf-8")):
            used.setdefault(m.group(1), []).append(vue.name)
    return used


def test_every_page_key_in_view_exists_in_both_locales():
    used = _used_page_keys()
    assert used, "视图里没扫到 page.* key——正则或目录布局变了，守卫会假绿"

    for lang in ("zh", "en"):
        have = _locale_page_keys(lang)
        missing = {k: sorted(set(v)) for k, v in used.items() if k not in have}
        assert not missing, (
            f"{lang}/admin.json 缺少视图在用的 page.* 词条（缺失时 t() 静默回退中文兜底，"
            f"英文界面会显示中文）：{json.dumps(missing, ensure_ascii=False)}"
        )
