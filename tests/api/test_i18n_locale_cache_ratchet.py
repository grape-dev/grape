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
import subprocess

import pytest

SHELL = pathlib.Path(__file__).resolve().parent.parent.parent / "src" / "api" / "frontend" / "shell"
LOCALES = SHELL / "locales"

LOCALE_FINGERPRINT = '0b8087089427aa51'

# 页面 → (当前 cacheVer, 历史下界=base 分支 arch-weekly-11 取值)
CACHE_VER = {
    'index.html': ('99', '53'),
    'admin.html': ('20261125-5', '20261114-23'),
    'register.html': ('76', '45'),
    'forgot.html': ('76', '45'),
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


def test_pages_keep_i18n_early_start_logic():
    """D1 防回归（QA Informational 2）：三页必须保留 i18n 早启隐藏赋值点。

    base 合并时曾把整段早启 IIFE 丢成裸 cacheVer 赋值（QA 架构守护 D1 BLOCKED），
    本断言确保 lang 探测 + i18n-loading 类注入不再被合并悄悄丢掉。
    """
    for page in ("index.html", "register.html", "forgot.html"):
        html = (SHELL / page).read_text(encoding="utf-8")
        assert "classList.add('i18n-loading')" in html, (
            f"{page} 丢失 i18n 早启隐藏逻辑（i18n-loading 注入点），"
            "请还原 lang 探测 IIFE——只留 cacheVer 赋值会让非 zh 用户首帧闪原文")


def test_locale_change_bumps_index_cachever():
    """D1 防回归补强（QA 复评：棘轮守卫盲区）：语言包内容变更必须同提交抬 cacheVer。

    既有守卫只比对「登记值 == 实际值」——指纹变了而渲染页 cacheVer 不动也能全绿，
    浏览器会继续吃旧语言包缓存，新增 key 取不到。本断言对比 HEAD~1：语言包指纹
    变了而 index.html 的 cacheVer 没变 → 红。浅克隆/单提交环境跳过。
    """
    # 仓库根：SHELL=repo/src/api/frontend/shell → 上溯 4 级；QA round-6 整改条件①
    # （此前 -C src/api 使 ls-tree 的 pathspec 空转，_fp_at 恒为空串哈希，守卫形同虚设）
    REPO_ROOT = SHELL.parent.parent.parent.parent

    def _git(*args) -> bytes:
        r = subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                           capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "ignore")[:200])
        return r.stdout

    try:
        head = _git("rev-parse", "HEAD").decode().strip()
        parents = _git("rev-list", "--parents", "-n", "1", "HEAD").decode().split()[1:]
    except Exception as e:
        pytest.skip(f"浅克隆 / 无 git 环境跳过: {e}")
    if not parents:
        pytest.skip("无前一提交可对比")
    # QA C-1（#2161 首轮）：merge 提交要对比全部 parent——base 侧 cacheVer 更高而
    # 语言包指纹变化时，仅比 HEAD~1 会漏报（守卫盲区本体）

    def _fp_at(commit: str) -> str:
        h = hashlib.sha256()
        for lang in ("zh", "en"):
            listed = _git("ls-tree", "-r", "--name-only", commit,
                          f"src/api/frontend/shell/locales/{lang}").decode().splitlines()
            for name in sorted(n for n in listed if n.endswith(".json")):
                h.update(f"{lang}/{name.rsplit('/', 1)[-1]}:".encode())
                h.update(_git("show", f"{commit}:{name}"))
        return h.hexdigest()[:16]

    def _ver_at(commit: str) -> str:
        html = _git("show", f"{commit}:src/api/frontend/shell/index.html").decode("utf-8")
        m = re.search(r"__I18N_CACHE_VER__\s*=\s*['\"]([^'\"]+)['\"]", html)
        return m.group(1) if m else ""

    v_head = _ver_at(head)
    # QA round-6 整改条件②：自证断言——_fp_at(head) 必须非空（zh/en 至少各 1 个 json），
    # 否则 fail 而非静默 continue，杜绝「空树哈希恒等」让守卫变成空转
    head_files = []
    for lang in ("zh", "en"):
        listed = _git("ls-tree", "-r", "--name-only", head,
                      f"src/api/frontend/shell/locales/{lang}").decode().splitlines()
        head_files += [n for n in listed if n.endswith(".json")]
    assert head_files, (
        "守卫自证失败：_fp_at 的 ls-tree 未列出任何语言包 json（cwd 或 pathspec 错误）"
        "——指纹恒等空转，本守卫不具备检出能力（QA round-6 条件②）")
    fp_head = _fp_at(head)
    for parent in parents:
        if _fp_at(parent) == fp_head:
            continue  # 该 parent 侧语言包没变，无需抬档
        v_parent = _ver_at(parent)
        assert version_rank(v_head) > version_rank(v_parent), (
            f"语言包内容相对 {parent[:9]} 发生变化但 index.html 的 i18n cacheVer 未抬升"
            f"（parent={v_parent} -> head={v_head}）——浏览器会继续吃旧缓存，"
            "新增 key 取不到（QA D1 盲区，merge 场景对两侧 parent 均需抬档）")


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
I18N_JS_VER = ('20261120-4', '20261114-23')  # (当前, base 下界)


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
