"""重算前端 chunk 源指纹棘轮（tests/api/test_frontend_bundle_source_ratchet.py）。

QA G-4 补口：该棘轮此前无生成器覆盖，抬档只能手抄两张大表。本脚本按当前真实树：

1. 用棘轮自身的解析口径算出每个 chunk 的 (页面版本, 注入 CSS 版本, 源指纹, 源清单, CSS 指纹)；
2. **源/样式确实变了**的 chunk：抬页面 `?v=`（所有引用该 chunk 的壳页 +1）、抬 entry 里
   运行时注入的 `?v=`（+1，未注入的跳过）；
3. 以抬档后的真实树重写 `FROZEN` 与 `SHIPPED`（健康树上两表逐字段相同）。

纪律：只允许「源变了才抬档」，不许在源未变时重冻指纹（那等于把守卫废掉）。

用法：
    python scripts/recompute_frontend_bundle_ratchet.py [--dry-run]
"""
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEST_FILE = REPO / "tests" / "api" / "test_frontend_bundle_source_ratchet.py"
SHELL = REPO / "src" / "api" / "frontend" / "shell"


def _load_ratchet():
    spec = importlib.util.spec_from_file_location("bundle_ratchet", TEST_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bump_page_versions(mod, bundles: dict, targets: list) -> dict:
    """抬所有引用目标 chunk 的壳页 ?v=（+1）；返回 {chunk: (旧, 新)}。"""
    bumped = {}
    pages = sorted(SHELL.glob("*.html"))
    texts = {p: p.read_text(encoding="utf-8") for p in pages}
    for bundle in targets:
        per_page = mod._page_versions().get(bundle) or {}
        for ver in per_page:
            try:
                new = str(int(ver) + 1)
            except ValueError:
                raise SystemExit(f"{bundle}: 页面版本 {ver!r} 非数字，无法自动抬档")
            for page, text in texts.items():
                pat = re.compile(re.escape("/" + bundle) + r"\?v=" + re.escape(ver) + r"(?=[\"'])")
                text = pat.sub("/" + bundle + "?v=" + new, text)
                texts[page] = text
            bumped[bundle] = (ver, new)
    for page, text in texts.items():
        if text != page.read_text(encoding="utf-8"):
            page.write_text(text, encoding="utf-8")
    return bumped


def _bump_inject_versions(mod, bundles: dict) -> dict:
    """抬 entry 运行时注入的 CSS ?v=（+1）；返回 {chunk: (旧, 新)}。"""
    bumped = {}
    for bundle, entry in mod._entries().items():
        if bundle not in bundles:
            continue
        need = bundles[bundle][4]  # css 指纹变化由调用方判定，这里只看是否需要抬
        if not need:
            continue
        text = entry.read_text(encoding="utf-8")
        m = mod.INJECT_VER.search(text)
        if not m:
            continue
        old = m.group(1)
        new = str(int(old) + 1)
        text = re.sub(
            r"(link\.href\s*=\s*'/static-vue/assets/'\s*\+\s*f\s*\+\s*'\?v=)" + re.escape(old),
            lambda mm: mm.group(1) + new, text, count=1)
        entry.write_text(text, encoding="utf-8")
        bumped[bundle] = (old, new)
    return bumped


def _fmt_src_list(srcs: list) -> str:
    return "[" + ", ".join(f'"{s}"' for s in srcs) + "]"


def _rewrite_tables(mod, actual: dict) -> None:
    frozen_lines = ["FROZEN = {"]
    shipped_lines = ["SHIPPED = {"]
    for bundle in sorted(actual):
        ver, css_ver, sha, srcs, css_sha = actual[bundle]
        frozen_lines.append(
            f'    "{bundle}": ("{ver}", {repr(css_ver) if css_ver else "None"}, "{sha}", '
            f"{_fmt_src_list(srcs)}),")
        shipped_lines.append(
            f'    "{bundle}": ("{ver}", {repr(css_ver) if css_ver else "None"}, "{sha}", '
            f"{repr(css_sha) if css_sha else 'None'}),")
    frozen_lines.append("}")
    shipped_lines.append("}")

    text = TEST_FILE.read_text(encoding="utf-8")
    text = re.sub(r"FROZEN = \{.*?\n\}\n", "\n".join(frozen_lines) + "\n", text,
                  count=1, flags=re.S)
    text = re.sub(r"SHIPPED = \{.*?\n\}\n", "\n".join(shipped_lines) + "\n", text,
                  count=1, flags=re.S)
    TEST_FILE.write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    dry = "--dry-run" in (argv if argv is not None else sys.argv[1:])
    mod = _load_ratchet()
    before = mod._bundles()

    page_targets, css_targets = [], []
    for bundle, row in before.items():
        old = mod.FROZEN.get(bundle)
        ver, css_ver, sha, srcs, css_sha = row
        if old is None:
            page_targets.append(bundle)          # 新 chunk：必须有页面引用才能登记
            continue
        f_ver, f_css_ver, f_sha, f_srcs = old
        if sha != f_sha or srcs != f_srcs:
            page_targets.append(bundle)
        shipped = mod.SHIPPED.get(bundle)
        if shipped and css_sha and shipped[3] and css_sha != shipped[3]:
            css_targets.append(bundle)

    print("源变化需抬页面 ?v= 的 chunk:", len(page_targets))
    for b in page_targets:
        print("  -", b)
    print("注入 CSS 指纹变化需抬注入 ?v= 的 chunk:", len(css_targets))
    for b in css_targets:
        print("  -", b)

    if dry:
        print("(dry-run：未改任何文件)")
        return 0

    bumped_pages = _bump_page_versions(mod, before, page_targets)
    bumped_inject = _bump_inject_versions(mod, {b: before[b] for b in css_targets})
    print("页面抬档:", bumped_pages)
    print("注入抬档:", bumped_inject)

    mod = _load_ratchet()          # 重读抬档后的真实树
    after = mod._bundles()
    _rewrite_tables(mod, after)
    print(f"FROZEN/SHIPPED 已按真实树重写（{len(after)} 个 chunk）")
    return 0


if __name__ == "__main__":
    sys.exit(main())