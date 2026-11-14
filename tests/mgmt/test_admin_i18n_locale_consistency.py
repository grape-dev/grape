"""管理台国际化一致性守卫（zh/en 语言包 + i18n.js 内联包 + 术语）。

背景（2026-11-14 用户质询"存在中英文配置关系反了的场景"）：语言包分两处——
`locales/<lang>/<module>.json`（异步加载的真源）与 `js/i18n.js` 的内联包（首帧兜底），
键集或语言标错都会表现为"中文界面出现英文 / 英文界面出现中文 / 回显原始 key"。
本守卫把可机检的四类问题钉住：

  1. en 侧不得出现中文字符（语言切换按钮类键除外）；
  2. 同一模块 zh/en 键集合一致（漏译/多键）；
  3. i18n.js 内联 admin.page 的键必须都存在于 locales 的真源里（防内联包漂移出孤儿键）；
  4. 术语守卫：不得回潮到「仓库平台 / 可驱动」与 "Repo Platforms / can drive"；
  5. 渠道显示名守卫：`configs/oauth-providers.json` 里的每个渠道在 zh/en 都必须有
     `provider_name` 词条（否则英文界面漏出库里的中文名）；
  6. 版本位守卫：加载 i18n.js 的每个壳页，其 `i18n.js?v=` / `auth.js?v=` 不得回退。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SHELL = ROOT / "src/api/frontend/shell"
CJK = re.compile(r"[\u4e00-\u9fff]")

# 语言切换按钮本身就写对方语言（"中文" 放在 en 包里是正常的）
_CJK_ALLOWED_KEYS = {"login.lang_switch_zh", "language.zh", "lang.zh"}


def _walk(obj, prefix="", out=None):
    out = out if out is not None else {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, str):
        out[prefix] = obj
    return out


def _load(path: Path) -> dict:
    return _walk(json.loads(path.read_text(encoding="utf-8")))


def _modules(lang: str):
    return sorted((SHELL / f"locales/{lang}").glob("*.json"))


def test_en_packs_have_no_chinese():
    bad = []
    for f in _modules("en"):
        for k, v in _load(f).items():
            if CJK.search(v) and k not in _CJK_ALLOWED_KEYS:
                bad.append(f"{f.name}:{k}={v[:40]}")
    assert not bad, f"en 语言包含中文（语言标错/误写）: {bad[:8]}"


def test_zh_en_key_sets_match_per_module():
    problems = []
    for f in _modules("zh"):
        en = SHELL / f"locales/en/{f.name}"
        if not en.exists():
            problems.append(f"{f.name}: 缺 en 语言包")
            continue
        zh_keys, en_keys = set(_load(f)), set(_load(en))
        if zh_keys != en_keys:
            problems.append(f"{f.name}: zh-only={sorted(zh_keys - en_keys)[:5]} en-only={sorted(en_keys - zh_keys)[:5]}")
    assert not problems, "zh/en 键集不一致: " + "; ".join(problems[:5])


def test_inline_pack_page_keys_exist_in_locales():
    text = (SHELL / "js/i18n.js").read_text(encoding="utf-8")
    # 内联包里的 admin.page 段：取两次 page: { 之间的键（zh/en 各一段）
    inline = set()
    for m in re.finditer(r"page: \{(.*?)\n        \},", text, re.S):
        inline |= {k for k in re.findall(r"^\s+(\w+):", m.group(1), re.M)}
    assert inline, "未能解析 i18n.js 内联 page 段"
    zh = set(_load(SHELL / "locales/zh/admin.json").get("page", {}))
    # _load 返回扁平键，page 段取顶层前缀
    zh_page = {k for k in zh if not k.startswith("page.") or "." not in k}
    zh_flat = json_deep_page_keys()
    orphan = inline - zh_flat
    assert not orphan, f"i18n.js 内联 page 键在语言包里不存在（孤儿键/拼写漂移）: {sorted(orphan)[:8]}"


def json_deep_page_keys():
    d = json.loads((SHELL / "locales/zh/admin.json").read_text(encoding="utf-8"))
    return set(d.get("page", {}))


def test_terminology_guard():
    zh = (SHELL / "locales/zh/admin.json").read_text(encoding="utf-8")
    en = (SHELL / "locales/en/admin.json").read_text(encoding="utf-8")
    assert "仓库平台" not in zh, "文案回潮：应使用「代码托管平台」"
    assert "可驱动" not in zh, "文案回潮：关系应表述为「可接入」"
    assert "Repo Platform" not in en, "en 回潮：应使用 Code Hosting Platform"
    assert "can drive" not in en, "en 回潮：关系应表述为 can access"

def test_inline_pack_language_direction():
    """i18n.js 内联包的中英两段不得互换（本批实锤：脚本 URL 版本位没 bump，浏览器吃到旧包，
    中文界面显示英文、英文界面显示中文）。"""
    text = (SHELL / "js/i18n.js").read_text(encoding="utf-8")
    zh_start = text.index("    zh: {")
    en_start = text.index("    en: {", zh_start)
    zh_seg, en_seg = text[zh_start:en_start], text[en_start:]

    def page_kv(seg):
        i = seg.index("        page: {")
        j = seg.index("\n        },", i)
        return dict(re.findall(r'(\w+):\s*"([^"]*)"', seg[i:j]))

    zh, en = page_kv(zh_seg), page_kv(en_seg)
    assert zh and en, "未能解析内联 page 段"
    # 抽样：这些键在 zh 段必须是中文、在 en 段不得含中文
    # 抽样键跟着 UI 走：配对分节（drive_label / self_domain_short / can_drive_hint）已随
    # "可接入的代码托管平台"整段移除，改用仍在详情页里出现的中文键做同一个断言。
    for key in ("sec_credential", "keep_hint", "save_creds", "enabled"):
        assert key in zh and key in en, f"内联包缺 {key}"
        assert CJK.search(zh[key]), f"zh 段 {key} 不是中文：{zh[key]!r}"
        assert not CJK.search(en[key]), f"en 段 {key} 含中文：{en[key]!r}"


def test_shell_script_cache_versions_bumped():
    """i18n.js 是内联包的真载体，改文案必须 bump 它的脚本 URL 版本与语言包键。"""
    html = (SHELL / "admin.html").read_text(encoding="utf-8")
    idx = (SHELL / "index.html").read_text(encoding="utf-8")
    shells = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(SHELL.glob("*.html"))]
    for name, text in shells:
        m = re.search(r"i18n\.js\?v=([0-9]+-[0-9]+)", text)
        if m is None:
            continue  # 该页不加载 i18n.js（如 beta-apply 静态页）
        ver = tuple(int(x) for x in m.group(1).split("-"))
        assert ver >= (20261114, 12), f"{name} 的 i18n.js 版本位回退/未 bump：{m.group(1)}"
    # auth.js 承载 I18N.init（语言包缓存键），改它同样必须 bump
    for name, text in shells:
        for m in re.finditer(r"auth\.js\?v=(\d+)", text):
            assert int(m.group(1)) >= 8, f"{name} 的 auth.js 版本位未随 I18N.init 改动 bump"
    init = re.search(r"I18N\.init\('([0-9]+-[0-9]+)'\)", html)
    assert init and tuple(int(x) for x in init.group(1).split("-")) >= (20261114, 12), \
        "admin.html 的语言包缓存键未 bump"


def test_every_configured_provider_has_a_localised_name():
    """渠道显示名走 admin.provider_name：配置里每加一个渠道，中英两个词条都必须补齐，
    否则英文界面会直接漏出库里存的中文名（华为云 → 华为云，实锤过）。"""
    cfg = json.loads((ROOT / "configs/oauth-providers.json").read_text(encoding="utf-8"))
    providers = [k for k in cfg if not k.startswith("_")]
    assert providers, "configs/oauth-providers.json 里没解析到渠道"
    for lang in ("zh", "en"):
        pack = json.loads((SHELL / f"locales/{lang}/admin.json").read_text(encoding="utf-8"))
        names = pack.get("provider_name") or {}
        missing = [p for p in providers if not names.get(p)]
        assert not missing, f"{lang} 语言包缺 provider_name：{missing}"
