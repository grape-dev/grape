"""i18n 语言包缓存版本 ratchet（架构守护第 2 轮复审 C-1）。

回归背景：语言包的 URL 是 `/static/locales/<lang>/<mod>.json?v=<cacheVer>`
（`shell/js/i18n.js`：`_cacheVer` ← 页面的 `window.__I18N_CACHE_VER__`，admin 走
`I18N.init('<ver>')`）。语言包内容变了而 cacheVer 没动，老客户端会继续吃浏览器里
的旧 JSON —— 新增 key 渲染成裸 key 或回退到 inline 最小包（templates 流水线阶段 /
标签亮点键就是这么丢的）。

人工纪律不可靠，本守卫把两个事实绑在一起、任一变都必须同时改：
  1. `LOCALE_FINGERPRINT`：locales/{zh,en} 全量内容指纹 —— 内容一变就红；
  2. `CACHE_VER`：四页当前 cacheVer + 历史下界 —— **只升不降**，即使四页一起降也拦。

纪律：改 locales/** → bump 对应页面 cacheVer 并同步更新这里；改 cacheVer → 同步这里。
"""
import hashlib
import pathlib
import re

SHELL = pathlib.Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "shell"
LOCALES = SHELL / "locales"

LOCALE_FINGERPRINT = "3e42dba7dd060b1a"

# 页面 → (当前 cacheVer, 历史下界=base 分支 arch-weekly-11 取值)
CACHE_VER = {
    "index.html": ("84", "53"),
    "admin.html": ("20261122-2", "20261114-23"),
    "register.html": ("65", "45"),
    "forgot.html": ("65", "45"),
}

# 各页面 cacheVer 的取法（三份 auth 页是内联 IIFE，admin 走 I18N.init）
PAGE_PATTERN = {
    "index.html": r"__I18N_CACHE_VER__\s*=\s*['\"]([^'\"]+)['\"]",
    "admin.html": r"I18N\.init\(\s*['\"]([^'\"]+)['\"]\s*\)",
    "register.html": r"__I18N_CACHE_VER__\s*=\s*['\"]([^'\"]+)['\"]",
    "forgot.html": r"__I18N_CACHE_VER__\s*=\s*['\"]([^'\"]+)['\"]",
}


def locale_fingerprint() -> str:
    """locales/{zh,en}/*.json 的内容指纹（路径 + 字节全进哈希）。"""
    h = hashlib.sha256()
    for lang in ("zh", "en"):
        for f in sorted((LOCALES / lang).glob("*.json")):
            h.update(f"{lang}/{f.name}:".encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def version_rank(v: str) -> tuple:
    """把 cacheVer 归一化成可比较的序。

    支持两种形态：纯数字（'43'）与日期序号（'20261113-1'）。
    统一成 (0, 日期部分, 序号) / (1, 整数) —— 精度足够做「只升不降」判断。
    """
    m = re.fullmatch(r"(\d{8})-(\d+)", v)
    if m:
        return (0, int(m.group(1)), int(m.group(2)))
    if re.fullmatch(r"\d+", v):
        return (1, int(v), 0)
    raise AssertionError(f"无法解析的 cacheVer: {v!r}（只允许纯数字或 YYYYMMDD-N）")


def page_cache_ver(page: str) -> str:
    html = (SHELL / page).read_text(encoding="utf-8")
    m = re.search(PAGE_PATTERN[page], html)
    assert m, f"{page} 未找到 cacheVer（i18n 语言包 URL 缓存键缺失）"
    return m.group(1)


def test_locale_fingerprint_matches_recorded():
    """语言包内容变了就必须 bump cacheVer 并更新指纹。"""
    actual = locale_fingerprint()
    assert actual == LOCALE_FINGERPRINT, (
        f"locales 内容指纹变化（记录的 {LOCALE_FINGERPRINT} vs 实际 {actual}）："
        "语言包已改动 → 请 bump 四个页面的 cacheVer，并把本文件 LOCALE_FINGERPRINT "
        "改成新指纹（同一次提交）"
    )


def test_page_cache_ver_is_current_and_monotonic():
    """四页 cacheVer 必须等于登记值，且不低于基线分支的历史下界。"""
    bad = []
    for page, (current, floor) in CACHE_VER.items():
        got = page_cache_ver(page)
        if got != current:
            bad.append(f"{page}: 登记={current} 实际={got}（改过就同步本文件）")
        elif version_rank(got) < version_rank(floor):
            bad.append(f"{page}: 倒退 实际={got} < 下界={floor}（只升不降）")
    assert not bad, "i18n 语言包 cacheVer 校验失败: " + "; ".join(bad)


# shell/js/i18n.js 自身的脚本缓存键：日期型版本，同样「只升不降」
# （复审 R-1：棘轮只认纯数字，这类版本此前没有任何下界检查）
I18N_JS_VER = ("20261117-1", "20261114-23")  # (当前, base 下界)


def test_i18n_js_script_version_monotonic():
    """四页引用 shell/js/i18n.js 的 ?v= 必须等于登记值且不低于 base。"""
    current, floor = I18N_JS_VER
    bad = []
    for page in CACHE_VER:
        html = (SHELL / page).read_text(encoding="utf-8")
        vers = set(re.findall(r"i18n\.js\?v=([0-9A-Za-z.\-]+)", html))
        if vers != {current}:
            bad.append(f"{page}: i18n.js?v={sorted(vers) or '缺失'}，登记={current}")
        elif version_rank(current) < version_rank(floor):
            bad.append(f"{page}: 倒退 {current} < {floor}")
    assert not bad, "i18n.js 脚本版本校验失败: " + "; ".join(bad)


def test_locale_url_carries_cache_ver():
    """机制自证：语言包 URL 必须把 cacheVer 拼进 ?v=（否则上面的版本号无意义）。"""
    js = (SHELL / "js" / "i18n.js").read_text(encoding="utf-8")
    assert "/static/locales/" in js, "i18n.js 语言包基址不在 /static/locales/"
    assert re.search(r"'\?v='\s*\+\s*_cacheVer|_cacheVer\s*\?\s*'\?v=", js), (
        "i18n.js 未把 _cacheVer 拼到语言包 URL 上，cacheVer 不生效"
    )
