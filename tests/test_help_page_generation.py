"""Tests for src/help_page_gen.py (v2: help.html SPA shell; content from help/ dir).

Verifies the generator renders help/usage + help/api markdown into src/api/static/help.html:
- output is UTF-8 without BOM, cleanly decodable
- embedded data parses; usage + api docs with html/toc/title/id
- landing offers 使用文档 / API 文档 ; section scoping labels present
- every doc's first H1 appears; heading ids unique across all docs
- same-level sub-TOC items share the same level (2.1/2.2 not nested)
- rendered structures (code fence / table) survive; generation deterministic
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import src.help_page_gen as gen

REPO_ROOT = Path(__file__).resolve().parent.parent
HELP_HTML = REPO_ROOT / "src" / "api" / "static" / "help.html"
API_FILES = sorted(p.name for p in gen.API_DIR.glob("*.md"))


def _page_text() -> str:
    return HELP_HTML.read_text(encoding="utf-8")


def _load_data() -> dict:
    text = _page_text()
    m = re.search(
        r'<script type="application/json" id="help-data">(.*?)</script>', text, re.DOTALL
    )
    assert m, "help-data script missing"
    return json.loads(m.group(1))


@pytest.fixture(name="page_text")
def _page_text_fixture() -> str:
    return _page_text()


def _first_h1(path: Path) -> str:
    md = path.read_text(encoding="utf-8")
    line = next((l for l in md.splitlines() if l.startswith("# ")), "")
    return line.lstrip("# ").strip()


@pytest.fixture(name="data")
def _data_fixture() -> dict:
    return _load_data()


class TestOutputFile:
    def test_file_exists_and_utf8_no_bom(self):
        raw = HELP_HTML.read_bytes()
        assert raw[:3] != b"\xef\xbb\xbf", "help.html has BOM"
        raw.decode("utf-8")

    def test_no_replacement_chars(self, page_text):
        assert "\ufffd" not in page_text

    def test_contains_migration_note(self, page_text):
        assert ("生成产物" in page_text or "生成物" in page_text) and "help/" in page_text


class TestShell:
    def test_landing_two_options(self, page_text):
        assert "使用文档" in page_text and "API 文档" in page_text

    def test_routes_present(self, page_text):
        assert "#/usage" in page_text and "#/api" in page_text
        assert "#/developer" in page_text, "Issue #373: developer section route missing"

    def test_section_scoping_labels(self, page_text):
        assert "使用指南" in page_text and "API 参考" in page_text
        assert "开发者文档" in page_text, "Issue #373: developer section label missing"

    def test_openapi_download_link_fixed(self, page_text):
        """契约下载 URL 固定为 /static/openapi.json（生成物，启动期落盘）。"""
        assert "下载 openapi.json" in page_text
        assert "/static/openapi.json" in page_text

    def test_data_parses_with_correct_counts(self, data):
        assert len(data["usage"]) == len(gen.USAGE_ORDER), \
            f"expected {len(gen.USAGE_ORDER)} usage docs, got {len(data['usage'])}"
        assert len(data["api"]) == len(API_FILES), \
            f"expected {len(API_FILES)} api docs, got {len(data['api'])}"
        assert len(data.get("developer", [])) == len(gen.DEVELOPER_ORDER), \
            f"expected {len(gen.DEVELOPER_ORDER)} developer docs, got {len(data.get('developer', []))}"

    def test_every_doc_has_fields(self, data):
        for section in ("usage", "api", "developer"):
            for d in data[section]:
                assert d["id"] and d["title"], f"bad id/title in {d}"
                assert isinstance(d["html"], str) and len(d["html"]) > 50
                assert isinstance(d["toc"], list)


class TestContent:
    @pytest.mark.parametrize("filename", gen.USAGE_ORDER)
    def test_usage_first_h1_present(self, filename, data):
        title = _first_h1(gen.USAGE_DIR / filename)
        html_all = "".join(d["html"] for d in data["usage"])
        assert title in html_all, f"H1 '{title}' of {filename} missing"

    @pytest.mark.parametrize("filename", API_FILES if API_FILES else ["README.md"])
    def test_api_first_h1_present(self, filename, data):
        title = _first_h1(gen.API_DIR / filename)
        html_all = "".join(d["html"] for d in data["api"])
        assert title in html_all, f"H1 '{title}' of {filename} missing"

    @pytest.mark.parametrize("filename", gen.DEVELOPER_ORDER)
    def test_developer_first_h1_present(self, filename, data):
        title = _first_h1(gen.DEVELOPER_DIR / filename)
        html_all = "".join(d["html"] for d in data["developer"])
        assert title in html_all, f"H1 '{title}' of {filename} missing"

    def test_heading_ids_unique_across_docs(self, data):
        ids = []
        for section in ("usage", "api", "developer"):
            for d in data[section]:
                ids += re.findall(r'id="([^"]+)"', d["html"])
        assert ids, "no heading ids found"
        assert len(ids) == len(set(ids)), "duplicate heading ids across docs"

    def test_sub_toc_same_level_siblings(self, data):
        """quickstart 的 2.1/2.2/2.3/2.4 必须同级（level==2），不得相互嵌套。"""
        doc = next(d for d in data["usage"] if d["id"] == "quickstart")
        levels = [r["level"] for r in doc["toc"]]
        assert levels, "quickstart has no sub-toc"
        assert all(l == 2 for l in levels), f"quickstart sub-headings not all sibling level2: {levels}"


class TestRenderedStructure:
    def test_code_block_rendered(self, data):
        html_all = "".join(d["html"] for sec in data.values() for d in sec)
        assert "<pre><code" in html_all

    def test_table_rendered(self, data):
        html_all = "".join(d["html"] for sec in data.values() for d in sec)
        assert "<table>" in html_all

    def test_md_links_rewritten_to_spa_routes(self, data):
        """README 的相对 .md 链接须改写为 SPA 路由（否则点成 /static/xx.md 404）。"""
        readme = next(d for d in data["api"] if d["id"] == "README")
        assert 'href="#/api/00-auth"' in readme["html"], "00-auth link not rewritten"
        assert 'href="#/api/12-a2a"' in readme["html"], "12-a2a link not rewritten"
        assert re.search(r'href="(?!https?://|/|#)[^"]*\.md', readme["html"]) is None, \
            "remaining non-rewritten relative .md links in README"

    def test_external_links_untouched(self, data):
        """外部 http/https 链接保持原样。"""
        readme = next(d for d in data["api"] if d["id"] == "README")
        html = readme["html"]
        for h in re.findall(r'href="([^"]+)"', html):
            if h.startswith("http"):
                assert not h.startswith("#/"), f"external link mangled: {h}"


class TestGenerate:
    def test_source_count(self):
        n = gen.generate()
        expected = len(gen.USAGE_ORDER) + len(API_FILES) + len(gen.REFERENCE_ORDER) + len(gen.DEVELOPER_ORDER)
        assert n == expected, f"expected {expected} source docs, got {n}"

    def test_repeatable_bytes(self):
        gen.generate()
        first = HELP_HTML.read_bytes()
        gen.generate()
        second = HELP_HTML.read_bytes()
        assert first == second, "generation not deterministic"


class TestDirectoryNumbering:
    """Issue #291: 左侧目录层级编号（1/1.1/1.1.1）+ 总览标识。"""

    def test_api_docs_numbered_1_to_n(self, data):
        api = data["api"]
        assert len(api) == len(API_FILES)
        readme = next(d for d in api if d["id"] == "README")
        assert readme["no"] == "" and readme.get("is_overview") is True, "README should be overview"
        numbered = [d for d in api if d.get("no")]
        assert len(numbered) == len(API_FILES) - 1
        assert [d["no"] for d in numbered] == [str(i) for i in range(1, len(numbered) + 1)], \
            "api docs must be numbered 1..N in order"

    def test_usage_docs_numbered_1_to_n(self, data):
        usage = data["usage"]
        assert len(usage) == len(gen.USAGE_ORDER)
        assert [d["no"] for d in usage] == [str(i) for i in range(1, len(usage) + 1)]

    def test_toc_hierarchical_numbers(self, data):
        """00-auth 小节应为 1.1 / 1.1.1 等层级编号（文档 1 + 层级序号）。"""
        auth = next(d for d in data["api"] if d["id"] == "00-auth")
        assert auth["no"] == "1"
        numbers = [r["no"] for r in auth["toc"]]
        assert numbers, "00-auth should have toc entries"
        assert numbers[0] == "1.1", f"first sub heading should be 1.1, got {numbers[0]}"
        assert any(re.match(r"^1\.\d+\.\d+$", n) for n in numbers), \
            f"expected nested number like 1.1.1, got {numbers[:6]}"

    def test_flat_siblings_incremented(self, data):
        """quickstart 同级 2.1/2.2/2.3/2.4 递增且不嵌套。"""
        qs = next(d for d in data["usage"] if d["id"] == "quickstart")
        numbers = [r["no"] for r in qs["toc"]]
        assert numbers[0] == "2.1"
        assert all(n.startswith("2.") and len(n.split(".")) == 2 for n in numbers), numbers

    def test_reference_numbered(self, data):
        ref = data.get("reference") or []
        assert all(d["no"] == str(i) for i, d in enumerate(ref, start=1))


class TestApiEndpointStandardization:
    """Issue #291: 右侧接口标准化字段（方法徽章/路径/参数/响应/错误码标签）。"""

    def test_js_has_endpoint_card_markers(self, page_text):
        for marker in ("ep-card", "ep-method", "ep-path", "ep-title", "ep-label", "decorateApi"):
            assert marker in page_text, f"missing marker {marker!r} in help JS"

    def test_js_has_method_regex(self, page_text):
        assert re.search(r"GET\|POST\|PUT\|DELETE\|PATCH\|HEAD\|OPTIONS", page_text)

    def test_js_has_directory_collapse_markers(self, page_text):
        for marker in ("subnav", "caret", "dno", "dn-t", "data-for"):
            assert marker in page_text, f"missing marker {marker!r}"

    def test_js_overview_badge(self, page_text):
        assert "总览" in page_text

    def test_endpoint_headings_parseable(self, data):
        """至少一个 API 端点的 H2 能被方法+路径正则解析（内容 <=> 展示契约一致）。"""
        pattern = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(\S+)")
        found = False
        for d in data["api"]:
            for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", d["html"], re.DOTALL):
                text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if pattern.match(text):
                    found = True
        assert found, "no endpoint heading matches METHOD /path pattern"